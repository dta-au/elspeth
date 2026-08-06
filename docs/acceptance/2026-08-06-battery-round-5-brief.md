# Brief: battery round 5 — verify the round-4 fix wave on the live Sydney stack

Date written: 2026-08-06. Author: `claude-r4-battery`. Audience: the session
that does it. Premise: every defect round 4 raised has a landed fix.

Read first:

1. `2026-08-06-battery-round-4-report.md` — per-graph results, cost, AWS ledger.
   **Four sections carry dated corrections. The corrections are the record; the
   original text is not.**
2. The fix commits' messages — `80f9d62af`, `e64fa38f2`, `3f3c929b0`,
   `53d06bea9`, `c15c7e2c6`, `e85adcce3`. Where a commit message and the
   round-4 report disagree about **mechanism**, the commit message wins: it was
   written after the investigation that refuted the report.
3. `docs/runbooks/aws-ecs-cold-install.md` — only §Redeploy applies. Round 5 is
   not a cold install.
4. `ops-local/README.md` — the environment of record and its five traps.

## What round 5 is for

Round 4 found three defects and could not settle four confirm targets. Round 5
answers four questions, in priority order:

1. Do the fixes hold **live, against the composer**, on criteria that would
   have failed at `52ab3ec8b`?
2. Does the fix wave **break anything else**? It touched sources, DAG
   validation, execution accounting, diagnostics, transforms *and the
   composer's own prompt* — the highest-blast-radius wave of the release.
3. Can a **full battery run at all**? Three of twelve composes died at the wall
   in round 4, which is the direct reason `elspeth-a79f1b2e6b` (cost) and
   `elspeth-902fc354b2` (g08 ×3) could not close.
4. What does the package's **own default** configuration do? `elspeth-52af290183`
   records 240s + `high` reasoning effort as the worst measured combination,
   never exercised. Round 4 ran at 270s + `medium` and so did not exercise it
   either.

### What round 5 is explicitly not for

- It is **not a cold install**. It produces **no evidence** for
  `elspeth-9f7d336e1c` and none for `elspeth-671a17d5c0`'s install path. Do not
  let the report read as having re-qualified the installer.
- It does not verify the advisor cluster per-ticket — see precondition P2.
- It does not owe coverage of the 70-ticket `verifying` queue. See
  §Proportionality.

## The framing correction that matters most

**Round 5 is not a controlled comparison, and the report must not present it as
one.**

Round 4's regression claims collapsed because the round moved two variables.
Round 5 reuses the stack — same Region, models, network, database, allowlist,
timeout — so it *looks* like a single-variable change. It is not. The fix wave
edited the composer's prompt:

| Authoring surface changed | Commit |
|---|---|
| `csv_source.py` / `json_source.py` `composer_hints` — header-normalization rule | `80f9d62af` |
| `type_coerce.py` / `value_transform.py` `composer_hints` — arrives-vs-output, sandbox-true capabilities | `53d06bea9` |
| `pipeline_composer.md` one-shot mapping row | `e64fa38f2` |
| `generation.py` numeric-aggregation repair text | `e64fa38f2` |

That moves authoring behaviour for **every graph in the corpus**, not only
g01/g02/g05. A graph that changes verdict in round 5 is confounded between "the
fix worked" and "the hints changed the composer's mind". The per-graph table is
a **change-detector**, not a controlled comparison.

**The one attribution lever available is the round-4 authored artefact.**
Round 3's were lost to teardown — precisely why round 4 could not attribute.
Do not repeat it, and note that **the driver does not currently capture it**:

`drive_graph.py` saves `compose.json`, `compose-reconciled.json`, `validate`,
`run`, `diagnostics`, `outputs`, `execute` and `resolve-*`. **None of those is
the authored pipeline.** `compose.json` is `{status, body:{message:{…}}}` — one
assistant message. Archiving `/tmp/elspeth-battery` preserves the transcript and
*not* the lever.

The authored artefact is a **live API read** and must happen before the
redeploy:

```
GET /api/sessions/{session_id}/state        # composition state
GET /api/sessions/{session_id}/state/yaml   # rendered pipeline
```

- Pull both for **every round-4 session id** (they are in the round-4 report's
  per-graph table) into `ops-local/acceptance/r4-preserve/`, the existing
  convention for irreplaceable evidence (`r3-preserve/` holds Singapore's).
- Add the same two reads to `drive_graph.py` so round 5 captures its own lever
  without a second manual pass. Round 6 will need it for exactly this reason.
- For every round-5 row, record whether the authored artefact **differs** from
  round 4's, and how. *Same authored shape, different outcome* is a code
  change. *Different authored shape* is the hints. Only the first supports a
  claim about the fix.

## Preconditions — complete before the stack is touched

**Do P4 first.** It is the only step that reads state which later steps
destroy or make unreachable: the certificate rotation invalidates the current
CA bundle, and the redeploy changes what `/state` returns. Everything else can
be reordered freely.

### P1. Pin the SHA; run the full suite on it

Pin the candidate SHA and record it in the report the way round 4 pinned
`52ab3ec8b`. On that exact SHA run, in the shared checkout:

```bash
pytest tests/ -n 12          # plain default selection IS the CI-equivalent run
elspeth-lints check
wardline scan . --fail-on ERROR --fail-on-inert \
  --trust-pack scripts.wardline_pack --allow-custom-packs --local-only
```

*Corrected 2026-08-06: `scripts/wardline_gate.py` was removed in `cdfcee61b`
after this brief was written; the wardline scan above is the gate of record
(exit 0 = clean and non-inert). Do not skip the gate because the script 404s.*

Scoped runs will not do. The PH3 `source_file_hash` gate, the DAG corpus
manifest contract and the whole-tree AST gates are cross-cutting and only fire
on the full selection. `80f9d62af` rotated the hash for five sources and
`53d06bea9` landed the corpus-manifest and registry rotation — if those three
are inconsistent, the image boots and the *corpus* lies, which is worse than a
crash.

### P2. Decide the advisor question before writing the plan for it

Round 4 reported the advisor FLAG rate unmeasurable. That has now been checked
twice over and it is worse than "not queried": **the verdict is absent from
every surface the battery can reach.** No row in `chat_messages` in any role
contains `verdict`, and no file in the round-4 archive
(`/tmp/elspeth-battery/**`) contains `verdict` or `flagged` — the archived
compose body is `{status, body:{message:{…}}}`, a single assistant message.

So there are exactly two honest options, and the round must pick one **before**
it runs:

- **(a) Land the verdict-persistence fix first**, then measure. This is the only
  path that produces a number, and it is the precondition for the 15-ticket
  advisor cluster ever being battery-verifiable.
- **(b) Record unmeasured again**, and state that the advisor cluster stays
  unverifiable by this instrument.

Do not write a plan that "measures the FLAG rate from archived bodies". That
was checked; the bodies do not carry it.

*Corrected 2026-08-06 (elspeth-c804e5e3bb): the premise "absent from every
surface the battery can reach" missed the log stream. The two checks covered
`chat_messages` and the `/tmp` archive; `composer.advisor_checkpoint_pass`
events (per-pass verdict, clean included —
`advisor_checkpoint_telemetry.py:36`, an ancestor of the round-4 image) were
in the round-4 CloudWatch web log group all along, and the round-4 FLAG rate
is retro-measured at **9 of 15 early-phase `flagged` (60%)** — see the
round-4 report's corrected advisor section. **P2 resolves as option (a) with
zero code change**: round 5 measures the FLAG rate from the same log surface,
plus the durable END-gate fact from `completion_gates` (`5166baab2`). Do not
choose (b), and do not build a persistence fix the instrument does not need.*

### P3. Regenerate the ALB certificate — this is the tightest constraint

The self-signed ALB certificate is valid **24 hours**, and because it is
self-signed the CA bundle *is* the leaf:

```
notBefore = Aug  5 19:09:01 2026 GMT
notAfter  = Aug  6 19:09:01 2026 GMT      # confirmed live 2026-08-06T00:13Z
```

After that every battery call fails TLS verification and reads as an outage.
`terraform -chdir=scenario-a apply` (plan first; confirm the certificate
resources are the **only** replacements), then re-run
`ops-local/acceptance/derive_env.py` — the old `r4-alb-ca.pem` will not
validate the new leaf.

Order the whole round by this constraint: build any new tooling **before**
regenerating, then spend the fresh 24 hours on the stack.

The stack's own cleanup deadline is **2026-08-09**.

*Corrected 2026-08-06: **do not regenerate — retire the self-signed path
instead.** The delegated Route53 zone `aws.foundryside.dev` survives every
teardown (the reality seam), and P4's lever is already captured (17/17
sessions in `r4-preserve/manifest.json`), so nothing left in the round
depends on the expiring leaf. Replace this precondition with:*

1. *Request a **DNS-validated ACM certificate** for
   `elspeth.aws.foundryside.dev` in **`ap-southeast-2`**. The existing ACM
   certificates for that name are region-bound to Singapore
   (`ops-local/README.md` §environment) and cannot attach to the Sydney ALB;
   the zone is delegated, so validation is one Route53 record and issuance is
   minutes.*
2. *ALIAS `elspeth.aws.foundryside.dev` → the Sydney ALB (the name currently
   points at the torn-down Singapore stack; repointing orphans nothing).*
3. *Attach via SNI: `aws elbv2 add-listener-certificates`, **not**
   `modify-listener` on the default certificate. Terraform hardwires the
   self-signed import as the listener default (`network.tf:206-218`); swapping
   the default out-of-band would be silently reverted by any later
   `terraform apply` (the arm-B ALB change is exactly such an apply), while an
   added SNI certificate is outside the module's management and survives.*
4. *Update `r4-env.json`: `public_url` → `https://elspeth.aws.foundryside.dev`,
   delete `ca_bundle`. `drive_graph.py` tolerates the absent key (`.get()` +
   file check) and the public chain needs no bundle. `derive_env.py`'s CA
   derivation is no longer needed.*
5. *AWS ledger row per mutation, as ever.*

*Consequences: the 24-hour clock is gone — the round is ordered by the
**2026-08-09 cleanup deadline** only, and every "certificate time in hand"
gate elsewhere in this brief (arm D) now reads "stack time in hand". Browser
trust becomes real, which makes `elspeth-49b467d91a` (frontend DOM, previously
not sampleable partly for self-signed browser trust) sampleable via Playwright
— optional, and never at the cost of arm A. The raw-ALB hostname keeps serving
the old leaf until 19:09Z and may simply be left to expire.*

### P4. Capture the attribution lever, then clear the credential cache

The cached credential is a **Singapore-era** account against the Sydney Aurora
in round 4's case; any stale cache 401s every call.

```bash
cp -a /tmp/elspeth-battery ops-local/acceptance/r4-preserve/transcripts/   # transcripts only
rm -rf /tmp/elspeth-battery
```

**Copying that directory is not the archive step.** It holds compose
transcripts, not authored pipelines. The lever is the two live `/state` reads
in §Framing, and they must happen while the round-4 stack is still serving.

## Part 1 — redeploy

Reuse run `700e19d5-7894-4087-9a04-25aca8047b26` in `ap-southeast-2`. Do **not**
tear down and do **not** cold-install: holding Region, models, network,
database and allowlist constant is worth more than a second installer datum,
and the installer question belongs to `elspeth-e54343d43b` regardless.

1. Build both images from a detached worktree at the pinned SHA
   (`ops-local/acceptance/r4_build_images.sh`), verify label/arch/uid-1654/
   imports/frontend-dist/`SESSION_SCHEMA_EPOCH`, and push to the Sydney ECR.
2. **Scan gate — this is a confirm target now.** `c15c7e2c6` rebased the
   cloudwatch-agent sidecar onto the app image's distroless digest. Resolve the
   `linux/amd64` child from `docker buildx imagetools inspect --raw` and scan
   *that* — the index digest returns `ScanNotFoundException` forever. Round 4:
   web child 0 findings, agent child **33**. If the agent child is now clean,
   `elspeth-878dedd7f5` is confirmed by construction.
3. Register the new web task definition with `build_td_r4_sydney.py`, which
   refuses to emit unless a normalised diff shows only the intended changes.
   Carry the same four out-of-band settings and the **same 270s / `medium`**
   as round 4 — parity is the point.
4. `SESSION_SCHEMA_EPOCH` is **45** at HEAD, the same value the stack was
   installed with. Expect no store recreation. **Verify at the doctor rather
   than assuming**; if it reports stale, the drop/recreate sequence is in
   `ops-local/README.md` and it destroys the round-4 evidence you were told to
   archive first.
5. Doctor on the candidate web task definition as a command override — the
   separate doctor family does not carry the profile grants. Grade the
   **essential** container, not `containers[0]`.
6. Re-upload `docs/exec-summary.pdf` to the granted prefix if the redeploy
   disturbed it, and confirm the g07 quarantine key is still **absent**.

## Part 2 — the arms

Three arms, run in this order. **Capture the session-id list per arm as the
battery runs.** Round 4's cost query had no arm discriminator; arm C changes
reasoning effort and will otherwise pollute the headline figure that
`elspeth-a79f1b2e6b` is judged on. Split by **session set**, never by key
presence.

### Arm A — parity (the comparable table)

270s, `medium` effort, **serial**, the 11-graph corpus from
`round3-graph-corpus.md` with verbatim intents. This arm alone produces the
per-graph table and the cost cohort comparable to rounds 3 and 4.

**Sampling ×3 is mandatory for g01, g02 and g08** (below). At round 4's 25% wall
rate, budget for losing 2–4 composes across the round and re-sampling them.

### Arm B — completeness (the full battery)

Re-compose **only** the graphs that hit the wall in arm A, at a raised ceiling.
Purpose: establish whether those graphs *ever* compose, and give
`elspeth-a79f1b2e6b` the full battery its criterion requires.

**The ceiling is not a free knob, and 270 is not a conservative setting — it is
the maximum the package permits.** `config.py:929-941` enforces

```
composer_timeout_seconds  ≤  composer_transport_idle_ceiling_seconds
                             − composer_transport_headroom_seconds
                          =  300 − 30  =  270
```

and 300 is not arbitrary either: it is the ALB's own `idle_timeout`
(`modules/scenario/network.tf:154`). The stack carries no `TRANSPORT` overrides,
so the package defaults apply. **Round 4 ran at the exact structural ceiling**,
and a naive `COMPOSER_TIMEOUT_SECONDS=600` is a **boot crash** at config
validation, not a warning.

Raising it is therefore a coordinated **three-place** change:

| Place | From | To (example) |
|---|---|---|
| ALB `idle_timeout` (`network.tf:154`) | 300 | 900 |
| `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` | 300 (default) | 900 |
| `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` | 270 | 840 |

Change all three or none. Raising the composer's wall past the ALB's idle
timeout inverts the ordering the validator exists to protect — the proxy aborts
before the composer reports, and an honest 422 becomes a dead connection.

*Corrected 2026-08-06: `f8873e2a9` (elspeth-f159d2394b, landed after this
brief) changed the third place's plumbing. `COMPOSER_TIMEOUT_SECONDS` is now
`var.composer_timeout_seconds` (default 240) with a **plan-time validation
hard-capped at (0, 270]** — the Terraform variable cannot express 840;
`terraform plan` fails by design. The three-place change is unchanged in
substance but the routes are now:*

1. *ALB `idle_timeout` 300 → 900: Terraform (still a literal in
   `network.tf`). **Plan-gate the apply**: the module also owns the task
   definition and service, and the round runs on `build_td_r4_sydney.py`'s
   out-of-band TD — confirm the plan touches only the ALB attribute and does
   not roll the service back to Terraform's own TD. (If the P3 correction's
   SNI certificate was added, the plan must also not touch listener
   certificates.)*
2. *Both env vars (`IDLE_CEILING` → 900, `TIMEOUT` → 840): through the TD
   builder's out-of-band overrides — the same path that set 270 in round 4.
   Leave `var.composer_timeout_seconds` untouched at 240; it is overridden by
   the TD and cannot follow anyway.*

*Also from `f8873e2a9`: WebSettings now emits a
`composer_turn_budget_underfunded` structured warning when the wall cannot
fund the configured turn budget. If it appears at arm C's 240s it is a
designed disclosure — record it as the package-default datum, not a defect.*

Record the arm-B configuration in the ledger as a **deliberate deviation from
the package**. Arm A stays at the package-permitted 270 so parity is intact.

**This reframes the wall as a finding in its own right.** It is not a
conservatively-chosen timeout; it is the ALB idle timeout minus headroom, and
25% of real composes exceed it. "Raise the timeout" is an infrastructure change,
not a config change. Related class: `elspeth-f159d2394b` (wall cannot fund the
configured turn budget; not operator-tunable). If arm B shows the graphs compose
fine at 840, the defect is that the shipped envelope cannot fund the shipped
corpus — file it.

Report arm B as its own cohort. For the cost ticket, report the **union** and
label it an upper bound — arm B sessions ran longer by construction.

### Arm C — the package's own defaults (one datum)

Two graphs, at **240s + `high`** — no timeout override, no effort override.
This is `elspeth-52af290183`'s "worst measured combination, never exercised",
and it is the configuration a real operator gets from a stock install. One
honest datum is worth more than the ticket's current zero.

Expect it to be worse. That is the finding, not a failure of the round.

### Arm D — guided rider (optional, gated)

`scripts/acceptance_battery.py guided-rider <attempts>` already exists and
drives `/api/sessions/{id}/guided/start` + `/reconcile`. Run it **only if** arms
A–C complete with certificate time in hand.

Be honest about what it is: a **completions-over-attempts smoke datum**, not
per-ticket verification of the nine-ticket guided cluster. Round 5 gives that
cluster its first live signal; it does not close any of it.

## Part 3 — confirm targets

Every criterion below is written to be **discriminating** — to fail at
`52ab3ec8b` and pass now. If a criterion would have passed before the fix, it
is not evidence.

### The two-level rule for LLM-mediated fixes

This is the most important methodological point in the round. **Two of the
three round-4 fixes do not make the failure go away — they make it disclosed
and repairable.** Success then runs through the model, so a single sample
proves nothing in either direction.

| Level | What it tests | Determinism | Samples |
|---|---|---|---|
| **1 — disclosure** | The mechanism the fix actually changed | Deterministic | ×1, and verifiable **off-stack** |
| **2 — outcome** | The composer acts on the disclosure and authors a working graph | Stochastic | **×3 minimum** |

Level 1 for all three fixes can be checked without spending stack time: the
hints change is a string property, the phase-1 suggestion is reachable from a
local YAML validate, and the config-time rejection is a unit-level property.
**Do Level 1 off-stack first** — if Level 1 fails, Level 2 is not worth a
compose.

### Round-4 defects

| Ticket | Level 1 (deterministic) | Level 2 (×3) |
|---|---|---|
| `elspeth-3664e213c4` — invented source discards 100% of rows | A declared name the header resolution cannot produce is **rejected at config time** with an error naming the normalized form and both escape hatches. Never a silent discard | The composer declares the normalized form (or generates lowercase headers) and g01 runs. **Note round 3's g01 green was composition luck, not a working path** |
| `elspeth-aed3b69cf0` — `type_coerce` edge rejected | The `graph_structure` failure now carries a **non-null `patch_node_options` suggestion** teaching arrives-vs-output. Round 4's identical error had `suggestion=None` | The composer self-corrects within the loop and g02 reaches `completed_with_failures` 4/1/1 by design. A first-attempt rejection is **not** a failure of this fix |
| `elspeth-18bcf7dd09` — `value_transform` hints overclaim | Every capability named in `composer_hints` has at least one sandbox-accepted expression | g05 no longer dead-ends on a forbidden string method. **`elspeth-9cd47dc933` is closed not-a-bug** — a compose that authors nothing correctly returns 200 with a null state |
| `elspeth-43f52d69a4` — accounting contradiction | `routing.discarded` and `discard_summary.total` **agree**, and a discarded row's reason is retrievable from a named surface | Reproduce a zero-succeeded-token run and read the reason end to end |
| `elspeth-878dedd7f5` — scan gate | Agent amd64 child scans **COMPLETE with 0 findings** | n/a — confirmed at push time |
| `elspeth-a9967c55ff` — teardown TD sweep | Runbook fix only; no live evidence this round | n/a |

**Driver fix required first:** round 4's driver equated HTTP 200 with "pipeline
authored" and reported a false `state_exists` failure on g05's honest decline.
On a 200 it must read `body["state"]` and report *composer declined* when null.
Fix before running, or g05 will be misreported again.

*Corrected 2026-08-06: the null-state half is landed — `drive_graph.py` now
reads `body["state"]` and exits 3 on a designed decline. Verify, don't re-fix.
The `/state` + `/state/yaml` capture reads (§Framing) are still absent and
remain pre-stack work.*

### Carried confirm targets

| Ticket | What must be true live |
|---|---|
| `elspeth-902fc354b2` | g08 clean **×3**. Round 4 got 2 — the third died at the wall. Measured 1-in-4 intermittent, so 3 is the floor, not a nicety |
| `elspeth-a79f1b2e6b` | Cost across a **full** battery — every graph produced a run. Judge on the arm A ∪ B union, labelled an upper bound. Round 4's 0.3215 was a floor from a partial battery and must not be treated as the answer |
| `elspeth-52af290183` | Arm C's datum. Currently zero live evidence either way |
| `elspeth-47fa7c01eb` | Already **PASS** in round 4 on its stated criterion. Re-confirm cheaply; the sibling concern (readable but uninformative) is `elspeth-43f52d69a4` |
| `elspeth-cd0f6a6cd9` | Already **PASS**. Re-confirm the ADR-036 shape survives the fix wave — `80f9d62af` touched `aws_s3_source.py` |
| `elspeth-cfcd333f83` | **UNSAMPLED** in round 4 (no `llm` transform was authored in g05). Needs a graph that actually authors one to be exercised at all |
| `elspeth-39118dd24f` (g11) | Inconclusive in round 4, confounded by the wall. Arm B is its only real chance |
| `elspeth-9d13900064`, `elspeth-82d4c5146c` | Not sampled in round 4. Sample if a graph reaches their shape; do not construct one at the cost of arm A |

Not sampleable through the API, as in round 4: `elspeth-49b467d91a` (frontend
DOM — needs Playwright, and the self-signed ALB certificate is an open question
for browser trust) and `elspeth-454892147c` (needs an induced provider failure;
no lever exists). **Report unsampled rather than faking either.**

## Part 4 — cost

Same read-only path as round 4, through the `database-bootstrap` task
definition with the transaction set `READ ONLY`.

**Every cost table must filter on the arm's session-id set.** Report:

| Cohort | Why |
|---|---|
| Arm A alone | The only figure comparable to rounds 3 and 4 |
| Arm B alone | Longer-running by construction |
| Arm C alone | Different reasoning effort; will inflate `reasoning_tokens` |
| A ∪ B union | The full-battery figure `elspeth-a79f1b2e6b` is judged on, labelled an upper bound |

Report USD per session and tokens by call index, with the **advisor line
separate** from the composer line. Reference points:

| | |
|---|---|
| R3 pre-cache-fix | USD 1.2915/session |
| R3 post-cache-fix | USD 0.4946/session |
| **Round 4 (Sydney)** | USD **0.3215**/session — floor, partial battery |
| Round-4 advisor (GLM-5) | USD 0.0175/call, 5.0% of total |

Established; do not re-derive: the cached region is **tools + system only**
(~31k), the schema carry-forward contributes zero, and cost scales with **call
count**, not per-call context growth.

## Proportionality — what the battery does and does not owe

The `verifying` queue is **70 tickets** (30 P1, 35 P2, 5 P3). The battery is a
scarce, expensive, stochastic instrument on a stack with a 24-hour certificate.
It is the right instrument for some of that queue and the wrong one for most:

| Cluster | Count (P1) | Is the battery the instrument? |
|---|---|---|
| schema / contract | 17 (4) | **Mostly no.** Unit-verifiable. The battery adds value only where the *composer* authors the shape — so sample via the corpus, do not build probes |
| advisor | 15 (8) | **Not until P2 is resolved.** The verdict reaches no surface the battery can read |
| compose loop | 11 (4) | **Yes.** The corpus covers it partially and is the only live surface |
| guided | 9 (6) | **Yes, but only as smoke** (arm D). Per-ticket verification needs a prober, not the rider |
| run observability | 6 (1) | **Yes.** g01's empty run is the natural probe |
| other / sink | 12 (7) | Case by case |

State this in the report. A round that quietly implies the battery covered the
queue is worse than one that names what it did not touch.

## Reporting

`docs/acceptance/2026-08-0X-battery-round-5-report.md`. Per-graph verdict table
**with run ids**, **plus an authored-artefact-differs-from-round-4 column** —
that column is what makes the table interpretable. Cost tables per arm. New
defects labelled `battery-2026-08-XX`. An **AWS ledger row for every mutation**.

**Recommend closes; do not close.** Operator sign-off closes, and the close
carries `close_commit release/0.7.2@<tip>`. Stochastic items never close on a
single pass — and after this round, "stochastic" includes g01 and g02.

## Five things rounds 3 and 4 learned the hard way

1. **Reproduce before believing a diagnosis — including your own.** Round 4
   filed two P1 regressions. Neither was a regression, and both mechanisms were
   wrong: g01 is header-case normalization dating to RC2, g02 is a missing
   repair suggestion on a *correct* rejection. Round 3's green on both was
   composition luck.
2. **A probe that passes can still be misread.** Round 4's
   `examples/transform_pipeline` probe passed because the example declares what
   *arrives*. It was evidence of correct behaviour on both sides, and it was
   reported as evidence of a validator bypass.
3. **A landed fix is not evidence.** Verify the mechanism the fix changed, not
   the symptom — and for LLM-mediated fixes, verify it at both levels.
4. **Hold the variable you intend to hold.** Round 4 believed it moved one
   variable and moved two. Round 5 knows it moves two; the authored-artefact
   diff is how it stays honest.
5. **A live repro expires.** Capture into the ticket while the instance is up.
   Here the clock is the certificate, not the teardown.
