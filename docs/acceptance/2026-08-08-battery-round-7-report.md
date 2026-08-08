# Battery round 7 — cold install + full corpus, report of record

Date: 2026-08-08. Author: `claude-r7-battery`.
Acceptance pin: **`51d3d26c9`** (`release/0.7.2`), `SESSION_SCHEMA_EPOCH` 47.
Stack: scenario A, run `700e19d5-7894-4087-9a04-25aca8047b26`, `ap-southeast-2`,
**cold-installed** 2026-08-08. Web TD `:8`.
Protocol: the round-5 brief, re-based — see §How it differs from round 5.
Evidence: `ops-local/acceptance/r7-preserve/` (per-session `/state`,
`/state/yaml`, transcripts, diagnostics), `r7-battery-A/`, `r7-aws-ledger.md`.

## Headline

- **The wall is gone.** 19/19 corpus composes completed inside the envelope —
  **zero wall deaths**, against ~18% in round 5's arm A and ~25% in round 4.
  g09 needed **377s**, structurally unreachable under the old 270s ceiling.
- **Two new P1 defects**, both real, both found *only* because this was a cold
  install against real PostgreSQL with `source:llm` authorised:
  `elspeth-74b795208f` and `elspeth-15c72686f2`.
- **g08 is 3/3 clean** with identical row accounting — round 5 got 1/3 and
  refuted closure.
- **Three of four owed demo-readiness actions discharged**; the fourth
  (`elspeth-85f3cc3022`) gets a definitive **split verdict** and must not close.
- **Cost: USD 0.3391/session** across a *complete* 19-compose battery.
- **Advisor FLAG rate: 19/22 early-phase flagged (86%)**, cross-validated
  against the database's own call count.
- **The guided rider's 10/10 is worth nothing** and this round proves it
  quantitatively: those sessions made **zero LLM calls**.

## What round 7 is

A cold install plus a full-corpus battery against the ~60 P1 fixes that landed
`verifying` since round 6. Unlike round 5 — which reused its stack and produced
*no* installer evidence — round 7 rebuilds from a torn-down account, so it is
live evidence for `elspeth-671a17d5c0` and `elspeth-9f7d336e1c`.

### How it differs from round 5 — the per-graph table is a change-detector

Round 7 moves at least **three** variables. The table below is not a controlled
comparison and must not be read as one:

| Variable | Round 5 | Round 7 |
|---|---|---|
| Stack | reused (round-4 install) | **cold install** — new Aurora, network, store |
| Product | `59cb6f75e` | `51d3d26c9` (~60 P1 fixes `verifying`) |
| Composer wall / ALB idle | arm A 270/300, B 840/900, C 240/300 | **840/900 — the package's shipped render** |
| `plugin_allowlist` | no `source:llm` | **`source:llm` authorised** |

That last row matters more than it looks, and it paid off immediately — see
`elspeth-15c72686f2`. Rounds 4 and 5 both recorded the composer "authoring
around" the llm-source mechanism; round 6 established it was **unauthorable**.
Authorising it exposed a real product defect on the second sample.

The one real attribution lever is the **authored artefact** (`/state` +
`/state/yaml`, captured per session and diffed against `r5-preserve/`). *Same
authored shape, different outcome* is a code change; *different shape* is the
hints, allowlist or wall.

### Why round 7 has one compose arm where round 5 had three

`e0d78882e` (`elspeth-09c91778f5`) moved the package defaults to 900s ALB idle
/ 840s composer wall. Round 5's arms existed to separate three wall settings;
two of them (270, 240) **no longer ship anywhere**, so replaying them would
spend real money measuring dead configurations.

`locals.tf:423` now wires the transport ceiling to `var.alb_idle_timeout_seconds`,
and a `validation` block rejects a wall above `ceiling − 30` at **plan** time.
The coordinated three-place change round 5 performed by hand — and warned to
"change all three or none" — is now structurally impossible to get wrong.
Verified live: ALB `idle_timeout` = 900, TD env = 840.

## Acceptance-pin gates

| Gate | Result |
|---|---|
| `pytest tests/ -n 12` | **38442 passed, 8 failed**, 66 skipped (17m11s) — all 8 re-run **green**. Product-clean |
| `elspeth-lints check --rules all --root src/elspeth` | **exit 1 — 256 findings** (233 stale allowlist entries, 24 per-file `max_hits`), all `trust_tier.tier_model` |
| `wardline scan --fail-on ERROR --fail-on-inert …` | **exit 0** — clean *and* non-inert, 66 recognized boundaries |

Gates ran in a **detached worktree at the pin**: three sibling sessions were
actively editing `src/elspeth/web/` when the round started.

### The 8 failures were an instrument artifact

All 8 were subprocess tests — 6 in `tests/unit/elspeth_lints/`, 2 shipped-example
launchers — each spawning an interpreter at **`<rootdir>/.venv/bin/python`**,
which the detached worktree lacked. `PYTHONPATH` correctly redirected
**in-process** imports, so 38,442 tests genuinely ran against the pin; but a
subprocess test resolves its interpreter **by path**, which `PYTHONPATH` cannot
influence. The harness was simultaneously correct for in-process tests and
broken for subprocess ones. Symlink restored → **9 passed, 1 xfailed**.

Recorded because reporting the raw count would have filed 8 phantom defects
against a release candidate.

### The lint line is a BASELINE, not a delta — and rounds 5/6's was vacuous

Rounds 5 and 6 both record `elspeth-lints check: exit 0`. Per `AGENTS.md` the
bare command defaulted to `--rules nothing` until 2026-08-07: it **ran zero
rules and exited 0**, a green that certified any tree. Those lines are vacuous,
and round 7 is the first round in the series to measure the gate at all.

So **256 is a baseline establishment, not growth** — there is no comparable
prior corpus. Exit 1 is the deliberate fail-closed state (`elspeth-13f0cc04fb`),
cleared once by the operator at package completion.

One finding worth naming: allowlist entry `allow_hits[159]` binds to
`web/composer/guided/steps.py`, **which no longer exists** — an entry outlived
its source file, and the loader refuses rather than silently accepting it.

**The 256 is a complete count, not a truncated one** — checked, because a
"Refusing to load" could plausibly short-circuit the rest of the load and make
this a bad baseline for future rounds to diff against. It does not: the refusal
scopes to that single entry, and findings span **8 distinct config files**
(`cli`, `contracts`, `core`, `engine`, `plugins`, `telemetry`, `tui`, `web`)
across **67 source files**.

## Cold-install fidelity — deviations and defects

Round 7 is the only cold install since round 4. Deviations recorded as
findings, not smoothed over. **Three of the five assume stack REUSE** — that is
the pattern.

| # | Step | Finding |
|---|---|---|
| 1 | §1 identity | The stop condition `test "$(aws configure get region --profile "$AWS_PROFILE")" = "$AWS_REGION"` **fails by construction**: every profile is configured `ap-southeast-1`, the deployment Region is `ap-southeast-2` |
| 2 | §4 images | `r4_build_images.sh` runs `docker logout` **before** `docker buildx imagetools inspect` — a registry read. The manifest-shape check 401s every time and can never have passed. Fixed in `r7_build_images.sh` |
| 3 | credentials | The recorded env-export recipe is **insufficient for an apply** — see below. This is how the 2026-08-07 apply died at 79/86 |
| 4 | trap 3 (stale image pin) | **Does not apply to a cold install.** Terraform rendered the schema-init doctor *at* the candidate digest; no `respin_td.py` needed. It is a stack-**reuse** trap and the runbook does not say so |
| 5 | S3 fixture | The step reads *"re-upload `docs/exec-summary.pdf` … **if the redeploy disturbed it**"* — a conditional that **never fires on a cold install**, where the bucket is simply empty. g07 failed as a result and read as a regression until diagnosed |
| 6 | ECR tags | The repo is `IMMUTABLE` and `acceptance-<run-id>` already resolves to round 4's image, so the tag must carry the SHA. Correct registry behaviour; the runbook does not mention it |

### Credentials: the recorded recipe cannot complete an apply

1. `elspeth-acceptance` is an `aws login` **`login_session`** profile. Its
   credentials live in the CLI-private `~/.aws/login/cache/`, **not** in
   `~/.aws/credentials` (verified: that file holds only `elspeth-installer` and
   `elspeth-iam-lifecycle`). Terraform's Go SDK cannot read that mechanism.
2. The recorded workaround (`export-credentials --format env`) hands Terraform
   a **static 15-minute credential** (measured 14.5 min on a fresh grant)
   against an Aurora creation that exceeds it. Sufficient for the shorter
   2026-08-07 destroy; **not** for an apply.
3. Fix: a `credential_process` profile delegating refresh back to the AWS CLI,
   so the CLI remains the single owner of the refresh grant.
4. **That then failed for the other half of the same problem.** The apply
   reached 85/86 and died in the `database_bootstrap` provisioner with
   `credential_process: exit status 254`. Two AWS providers × parallelism 10 →
   concurrent refreshes of one grant invalidating each other. Fixed with
   `flock`; stress-verified 8 concurrent resolutions, 8 successes. The resumed
   apply completed `1 added, 1 destroyed`, RC 0.

**Recoverable and clean**: the state write succeeded, there was no
`errored.tfstate`, 91 resources tracked, and the tainted `terraform_data`
re-planned as exactly `1 to add, 1 to destroy`.

**A diagnosability defect worth fixing:** the provisioner writes the AWS CLI's
stderr to `"$work/run.err"` under a directory its `trap … EXIT` deletes, so on
the failure path the *actual* AWS error is destroyed and only the sentinel
`database_bootstrap_run_failed` survives. The root cause had to be recovered
from an unrelated later command. **A failure path must not delete the only
evidence of why it failed.**

## Per-graph results — arm A, 19 composes + 3 re-samples

Wall deaths: **0/19**. `Wall s` is driver end-to-end, not compose time.

| Graph | rc | Wall | Run status | Tokens | Discards | Outcomes | Artifacts | vs R5 |
|---|---|---|---|---|---|---|---|---|
| g01-s1 | 0 | 163s | completed | 4 | 0 | success:4 | 1 | DIFFERENT |
| g01-s2 | 0 | 168s | completed | 4 | 0 | success:4 | 1 | DIFFERENT |
| g01-s3 | 0 | 135s | completed | 4 | 0 | success:4 | 1 | DIFFERENT |
| g02-s1 | 0 | 77s | completed_with_failures | 5 | 0 | failure:1, success:4 | 3 | SAME |
| g02-s2 | 0 | 94s | completed_with_failures | 5 | 0 | failure:1, success:4 | 3 | SAME |
| g02-s3 | 0 | 73s | completed_with_failures | 5 | 0 | failure:1, success:4 | 3 | SAME |
| g03-s1 | 1 | 231s | *no run* — validate rejected | — | — | — | — | DIFFERENT |
| g03-s2 | 1 | 149s | *no run* — validate rejected | — | — | — | — | DIFFERENT |
| g03-s3 | 0 | 299s | **failed** | 3 | 0 | failure:2, transient:1 | 0 | DIFFERENT |
| g04 | 0 | 177s | completed | 9 | 0 | success:6, transient:3 | 1 | DIFFERENT |
| g05 | 0 | 227s | completed | 12 | 0 | success:6, transient:6 | 1 | DIFFERENT |
| g06 | 0 | 191s | completed | 20 | 0 | success:15, transient:5 | 3 | DIFFERENT |
| g07 | 0 | 68s | **failed** — *unseeded fixture* | 2 | 0 | failure:2 | 2 | DIFFERENT |
| g08-s1 | 0 | 260s | completed | 12 | 0 | success:8, transient:4 | 1 | SAME |
| g08-s2 | 0 | 209s | completed | 12 | 0 | success:8, transient:4 | 1 | DIFFERENT |
| g08-s3 | 0 | 268s | completed | 12 | 0 | success:8, transient:4 | 1 | DIFFERENT |
| g09 | 0 | 377s | completed | 3 | 0 | success:3 | 1 | SAME |
| g10 | 0 | 203s | completed | 5 | 0 | success:5 | 1 | DIFFERENT |
| g11 | 1 | 120s | **completed** (recovered) | 11 | 0 | — | 1 | DIFFERENT |
| g07-s2 | 0 | 132s | completed_with_failures | 2 | 0 | success:1, failure:1 | 2 | *re-sample, seeded* |
| g11-s2 | 0 | 122s | **failed** | 7 | 0 | — | 0 | *re-sample* |
| g11-s3 | 0 | 115s | completed | 11 | 0 | — | 1 | *re-sample* |

Full session/run ids: `r7-preserve/analysis/per-graph.md`.

**The `vs R5` column is weakest on g11, and should not be read as composer
drift.** Round 5's g11 arm-B artefact was a *csv seed + `llm` transform* — the
"authored around the mechanism" shape — whereas round 7's is a genuine
`source:llm`. That is the **allowlist change**, not the composer changing its
mind, so the two artefacts are not on a comparable axis at all. The same
caution applies wherever a `DIFFERENT` verdict coincides with a plugin that was
unauthorable in round 5.

### Two rows that would have been misreported

**g11 `rc=1` is a PASS.** The driver died with
`NameResolutionError … Temporary failure in name resolution` — a **client-side**
DNS blip. The run id `9b257c47…` in the traceback proves `/execute` had already
succeeded; re-reading the run server-side returned `completed`, 1 row read, 1
processed, 0 rejected, 877-byte artifact. Taking `rc=1` at face value would have
recorded the corpus's single most important graph as a failure and left two
demo-blockers unresolved for a third round.

**g07 was never a regression.** `InvalidS3ObjectException` /
`s3_object_unreadable` — the object did not exist, because a cold install
creates an empty bucket (fidelity finding 5). Seeding the fixture and
re-driving returned the designed `completed_with_failures` 1/1 shape.
**Diagnosis confirmed by intervention.**

## New defects

### `elspeth-74b795208f` (P1) — branch-loss forensics kill the run on PostgreSQL

g03-s3 validated cleanly, then failed at runtime:

```
(psycopg.errors.StringDataRightTruncation) value too long for type character varying(64)
INSERT INTO coalesce_branch_losses …
```

`token_traversal.py:315` builds `f"quarantined:{error_detail}"` where
`error_detail` is a full dict repr (~150 chars observed) and writes it into
`schema.py:991` — `Column("reason", String(64))`, commented
`# failed / quarantined / error_routed / …`. The schema states the intended
vocabulary; the writer violates it. The sibling branch three lines up
(`"max_retries_exceeded"`) is correct, and `compute_error_hash(error_detail)`
sits immediately below — the right pattern already exists in the same function.

**Structurally invisible to the test suite: SQLite does not enforce
`VARCHAR(n)` length.** All 38,442 tests pass while real PostgreSQL rejects the
row. Only a cold install against Aurora can find this.

Blast radius: any pipeline losing a coalesce branch to quarantine. The branch
loss itself was handled correctly — **the audit write for it is what killed the
run**.

### `elspeth-15c72686f2` (P1) — extras firewall misses the llm SOURCE

g11-s2 authored `llm source → content_safety → line_explode → text sink
(schema mode fixed)`. **Both gates certified it valid** — persisted
`is_valid: True`, `/validate is_valid: True`, 0 errors — and then every row died
at the sink input preflight:

```
Sink 'text' input validation failed: 2 validation errors for TextSinkRowSchema
announcement_usage  Extra inputs are not permitted [extra_forbidden]
announcement_model  Extra inputs are not permitted [extra_forbidden]
. This indicates an upstream transform/source schema bug.
```

`69c6ad4b5` added a build-time Rule A mirror rejecting a producer's guaranteed
extras against a locked consumer — described then as *"a shape that previously
built green and then killed every row at the executor input preflight on row
1"*. That is this failure exactly. The mirror does not model the llm **source**
as a producer of guaranteed extras (`<name>_usage`, `<name>_model`).

Second-order: the sink declares `on_write_failure: "discard"`, but the failure
is at *input validation*, before any write — so the policy never engages and
the run fails outright (0 discards, 6 failed states).

**Only reachable this round**, because `source:llm` was unauthorable before.
Fixing the instrument defect immediately produced a real product defect.

## Confirm-target verdicts

| Ticket | Verdict | Evidence |
|---|---|---|
| `elspeth-878dedd7f5` — ECR scan gate | **PASS by construction** | Both amd64 children scan `COMPLETE`, **0 findings** (round 4: agent child **33**). Status `COMPLETE`, not `ScanNotFoundException` — data, not a failure to look |
| `elspeth-3664e213c4` — g01 header normalization | **PASS ×3** | 4 tokens, 0 discards, 4 successes each, on a fresh store. Two different authoring strategies (explicit normalized fields; `observed` mode) both survive where round 4 discarded 4/4 |
| `elspeth-aed3b69cf0` — g02 type_coerce edge | **PASS ×3** | Designed 4-success/1-failure, 3 sinks, **zero repair turns**, authored shape identical to round 5 (`SAME` ×3) |
| `elspeth-902fc354b2` / `elspeth-41bcaa882e` — g08 | **PASS ×3** | 12 tokens, **8 successes**, 0 discards, 1 artifact — *identical* accounting across three samples. Round 5: 1/3, closure refuted |
| `elspeth-09c91778f5` — envelope | **PASS** | 0/19 wall deaths. g09 at **377s** is unreachable under the old 270s wall; round 5's 265.7s success was luck and arm C's 240s death was the honest signal |
| `elspeth-85f3cc3022` — g03 | **SPLIT — do not close** | See below |
| `elspeth-afdf55a17c` — g11 multiline sink | **0/3 reproduced** | s1 and s3 each wrote a real 877-byte, 10-sentence artifact. The criterion was "0/N reproduced"; N=3 |
| `elspeth-d1602e4b90` — g05 | **Does not reproduce** | `completed`, 6 successes, 0 discards. Consistent with the envelope-death hypothesis, inconsistent with a hard regression |
| `elspeth-155947ca47` — persisted/validate agreement | **PASS** | `/state/yaml` returned **409** on both invalid g03 states |
| `elspeth-9595abb7b0` — diverted-row reason disclosure | **NOT EXERCISED** | Zero diversions across all g11 samples. The condition never arose |
| `elspeth-49b467d91a` (frontend DOM), `elspeth-454892147c` (induced provider failure) | **UNSAMPLED** | No lever exists through this instrument |

`elspeth-9595abb7b0` is deliberately **not** marked pass. "Nothing went wrong"
is not evidence that an error path discloses correctly — that is the
emptiness-graded-check trap.

### `elspeth-85f3cc3022` — the closing gate, answered

This ticket's `fix_verification` names round 7 as its gate: *"closure requires a
LIVE g03 re-run showing `set_pipeline` surfacing the rejection **and** the
compose loop repairing from it."*

| Half | Verdict |
|---|---|
| `af62478df` static parity | ✅ **Confirmed.** Persisted `is_valid=False` with the violation recorded, where round 6 had `is_valid=True`. Discriminating |
| Compose loop repairs | ❌ **Not met.** Told `is_valid=False` with a non-null suggestion; declared done anyway, 0 previews |

**Do not close.** And the residual is now *sharper* than filed: the ticket's own
root-cause defended the composer as "following its termination contract exactly
on the information it was given". That defence is retired — the information is
now correct and it terminates anyway. The remaining defect is that **the
composer reaches its terminal state on a composition it has been correctly told
is invalid.**

Two of three g03 samples reproduced an identical edge-contract shape with
different node ids, which reads **systematic**, not the "sometimes" in the title.

**Correction to the proposed fix:** it says *"make the composer
`preview_pipeline` before declaring done — it already does this on g11 and
g08"*. Measured `preview_pipeline` tool calls: g01-s1 **1**, g03-s1 **0**,
g03-s2 **0**, **g08-s1 0 — and that run completed**. Instrument control: tool
names are visible in these transcripts and g01-s1 proves the string is greppable
when present, so the zeros are data. Preview usage is **stochastic composer
behaviour, not a per-graph convention** — there is no existing behaviour to
copy, so the fix must be an *enforced gate*.

## Owed demo-readiness actions — all four discharged

| Action | Ticket | Result |
|---|---|---|
| Drive g05 once | `elspeth-d1602e4b90` | **Clean** — does not reproduce |
| g03 ×3 | `elspeth-85f3cc3022` | **Split verdict** above; do not close |
| g11 sink evidence | `elspeth-afdf55a17c` | **0/3 reproduced**; found `elspeth-15c72686f2` instead |
| g09 row count | (the "15 of 18" note) | **Does not reproduce** — 3/3 rows success, 18/18 states completed, 0 discards |

## Cost

Read-only path through the `database-bootstrap` task definition, transaction
`READ ONLY`. Cohorts split by session set, never by key presence.

| Cohort | Sessions | LLM calls | Total tokens | Reasoning | USD | USD/session |
|---|---|---|---|---|---|---|
| **A_corpus** | 19 | 120 | 6,240,241 | 31,660 | 6.4436 | **0.3391** |
| resamples | 3 | 19 | 901,051 | 2,628 | 0.9221 | 0.3074 |
| D_rider | 10 | **0** | — | — | — | — |
| ALL | 32 (22 with audit) | 139 | 7,141,292 | 34,288 | 7.3657 | 0.3348 |

Composer/advisor split on `A_corpus`: composer `au.anthropic.claude-sonnet-4-6`
100 calls / USD 6.0862 (94.5%); advisor `zai.glm-5` 20 calls / USD 0.3574
(**5.5%**, against round 4's 5.0%).

| Reference | USD/session |
|---|---|
| R3 pre-cache-fix | 1.2915 |
| R3 post-cache-fix | 0.4946 |
| R4 (Sydney) | 0.3215 — floor, **partial** battery |
| R5 arm A | 0.3077 — **3 wall deaths** |
| **R7 A_corpus** | **0.3391 — complete battery, 0 wall deaths** |

Read that comparison carefully: R5's arm A was *cheaper partly because three of
its composes died early*. R7 is the first figure in the series where every
corpus graph ran to completion, so a modest rise is the expected shape of
"nothing was cut short", not a cost regression.

## Advisor

**19/22 early-phase passes `flagged` = 86%** (25 checkpoint passes total, 22
distinct sessions, 3 `end`-phase).

Prior measurements: round 4 retro-measured 9/15 (60%), round 5 16/20 (80%).
**These are three point measurements, not a trend.** They come from different
corpora, different walls and different allowlists, and round 7's cohort
includes graphs the earlier rounds never composed. Reading 60 → 80 → 86 as a
rising line would repeat round 4's mistake of attributing movement across a
multi-variable change. What can be said is narrower and still useful: the FLAG
rate is high and stable in the 60–86% band, and it is measurable — which was
itself in doubt as recently as round 5's precondition P2.

Cross-validated across two independent surfaces: the session database records
**25** advisor LLM calls (20 corpus + 5 resample); CloudWatch records **25**
`composer.advisor_checkpoint_pass` events. Agreement between the billing
surface and the telemetry surface is what makes this a measurement rather than
a count.

## Arm D — the guided rider closes nothing, and now we can prove it

10/10 attempts returned HTTP 200 with `"result": "completed", "has_state": true`
— identical to round 5. It means nothing:

- Every session is **empty**: `version 1`, `is_valid False`, **0 sources, 0
  nodes, 0 outputs**.
- The cohort made **zero LLM calls** and incurred **zero cost**.

`has_state` **cannot be false on a 200**, because `/guided/start` seeds an empty
state — the field is structurally incapable of failing, which makes a 10/10
tally on it worthless as evidence about composition. Arm D is a no-regression
datum on session **start** and closes none of the nine-ticket guided cluster.
Per-ticket guided verification needs a prober, not the rider.

## Proportionality — what this round does NOT cover

The `verifying` queue is **116 bugs (60 P1, 49 P2, 7 P3)**; the operator's "~60
fixes" is the **P1** count, not the queue.

- It does **not** verify 116 tickets, or 60. Nineteen composes sample whatever
  shapes the composer authors from eleven fixed intents.
- The **guided cluster (9)** gets a start-only smoke datum that closes nothing.
- The **advisor cluster (15)** gets a FLAG-rate measurement, not per-ticket
  verification.
- Most **schema/contract** tickets are unit-verifiable and gain nothing here.
- `elspeth-454892147c` and `elspeth-49b467d91a` remain **unsampled** — no lever.

Anything not reached is listed as unsampled, never as passed.

## Recommendations

**Recommend closes; do not close.** Operator sign-off closes, carrying
`close_commit release/0.7.2@<tip>`. Stochastic items never close on one pass.

- **Recommend close:** `elspeth-878dedd7f5`, `elspeth-902fc354b2`,
  `elspeth-41bcaa882e`, `elspeth-09c91778f5`, `elspeth-afdf55a17c`,
  `elspeth-d1602e4b90`.
- **Recommend a second pass before close:** `elspeth-3664e213c4`,
  `elspeth-aed3b69cf0` (both clean ×3, but stochastic by nature).
- **Do not close:** `elspeth-85f3cc3022` (split verdict),
  `elspeth-9595abb7b0` (not exercised).
- **New, needs triage:** `elspeth-74b795208f`, `elspeth-15c72686f2` (both P1).
- **Runbook fixes owed** to `elspeth-671a17d5c0`: fidelity findings 1–6 above,
  particularly the three that assume stack reuse.

## AWS ledger

`ops-local/acceptance/r7-aws-ledger.md` — a row per mutation. The Route53
A-ALIAS repoint is an **operator** action and is recorded as such; agents get no
DNS write. The ACM certificate `451ab653…` survived the teardown and was
attached to the new listener via SNI (`IsDefault: False`), leaving Terraform's
self-signed default untouched — so there is **no 24-hour TLS clock** this round.
Stack cleanup deadline: **2026-08-12**.
