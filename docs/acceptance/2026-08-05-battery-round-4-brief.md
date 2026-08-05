# Brief: rebuild, redeploy, and run acceptance battery round 4

Date written: 2026-08-05. Author: `claude-r3-deploy`. Audience: the session that
runs round 4. Predecessors — read in this order:

1. `2026-08-05-battery-round-3-report.md` — per-graph results, the defect list,
   and the corrections an adversarial pass forced on it.
2. `2026-08-05-compose-token-cost-brief.md` **plus its addendum** — the cost
   analysis. The addendum overturns three of the brief's attributions; where
   they disagree, **the addendum is right**.
3. `2026-08-03-r3-rca-remediation-tracker.md` — AWS ledger and coordination.

## Starting state (verified 2026-08-05 04:58 UTC)

| | |
|---|---|
| Release tip | `release/0.7.2@47e65abec` |
| Deployed | `a-fa1b99c60192978b10f7-web:14`, release `6bcd69037` |
| Cluster / service | `acceptance-a-fa1b99c60192978b10f7-{cluster,service}` |
| `SESSION_SCHEMA_EPOCH` | **45 on both** — no store recreation needed |
| Textract grant | present on `web:14` |
| Reasoning effort | `ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT=medium` set |
| Composer model | `bedrock/global.anthropic.claude-sonnet-4-6` |
| Advisor model | `bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0` |

**Confirm all of this from live AWS before acting** — the stack moved through
five task-definition revisions during round 3 and may move again.

The stack carried a `CleanupDeadline` tag of `2026-08-06T00:00:00Z`. Nothing
enforces it (it is set by terraform and read by nobody), but check the stack
still exists before planning. If it is gone, round 4 begins with a cold install
(`docs/runbooks/aws-ecs-deployment.md`), not a redeploy.

## Why round 4 exists

Three things landed after `web:14` was cut, and one predates it but was never
exercised:

- **`47e65abec`** — fixes `elspeth-902fc354b2` (locked-input extras compared
  against definite arrivals rather than own emits). **Undeployed.** This is the
  round's primary confirm target; it was one of round 3's own findings.
- **`30616c3ee`** — dropped duplicated `plugin_hints`, compact render contexts.
- **`809022925`** — the cache fix, already verified live at **USD 0.4246 per
  session against a 1.3394 baseline**. Round 4 should confirm that holds across
  a full battery rather than the trial's sample.
- **The advisor is still Haiku.** The IAM grant for
  `global.anthropic.claude-sonnet-5` **is already applied** to the task role
  (three ARNs added 2026-08-05); only the env flip and a redeploy remain. See
  "Advisor" below — it is a decision, not a chore.

## Part 1 — rebuild and publish

Build from the exact tip, `linux/amd64`, extras `webui llm aws postgres`:

```bash
git -C .claude/worktrees/ecs-redeploy-r3 checkout --detach <TIP>
cd .claude/worktrees/ecs-redeploy-r3
docker buildx build --platform linux/amd64 \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=<TIP>" \
  --label "org.opencontainers.image.source=https://github.com/johnm-dta/elspeth" \
  --load --tag "elspeth:rc-<TIP:0:12>" .
```

Verify **before** pushing anything — revision label matches the tip, `linux/amd64`,
uid/gid `1654`, `--version`, and a container smoke that asserts the frontend
dist is present and world-readable, `boto3` imports (aws extras), `psycopg`
**and** `psycopg2` import (postgres extras), and `SESSION_SCHEMA_EPOCH`.

**GHCR** (optional, operator-facing): `ghcr.io/johnm-dta/elspeth-web:<tag>`.
Package is **private** and should stay that way unless the operator says
otherwise. `gh auth token --user johnm-dta | docker login ghcr.io -u johnm-dta
--password-stdin` works; issue it as its own command (a chained
login-tag-push is refused by the permission classifier). `docker logout ghcr.io`
after. The existing `RC7.2-050826` tag is built from `8e16e1833` and is now
behind the tip — do not reuse the tag.

**ECR** is what ECS deploys from: `559849758286.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-web`.
Tags are **IMMUTABLE**; pick a fresh transport tag.

## Part 2 — redeploy

Follow `docs/runbooks/aws-ecs-existing-service-redeploy.md`. Four traps, all of
which have bitten a previous round:

1. **The ECR scan gate reads the platform manifest, not the OCI index.** buildx
   publishes an index; `describe-image-scan-findings` on the index digest
   returns `ScanNotFoundException` forever. Resolve the `linux/amd64` child from
   `docker buildx imagetools inspect --raw` and scan **that**.
2. **The runbook's step-6 `RUNNING_DIGEST = SCAN_DIGEST` check is
   unsatisfiable** for a buildx push, for the same reason — ECS reports the
   index digest. Verify the *chain* instead: task-definition pin == running
   digest, and the index's amd64 child == the scanned digest. (Recorded as a
   doc defect in the R3 tracker.)
3. **`RegisterTaskDefinition` is condition-gated** on
   `aws:RequestTag/ACCEPTANCE_RUN_ID`. Replicate the live task-definition tags
   into the registration or it is AccessDenied by design. Tags sit *beside*
   `taskDefinition` in the describe response but *inside* it in the register
   input.
4. **New `ELSPETH_WEB__` settings are atomic with the image.** `WebSettings`
   refuses unknown prefixed variables (`config.py:1134`), so a setting the
   running image does not know crashes the container at boot. Any new env must
   land in the **same** revision as the image that understands it.

Derive the candidate task definition from the live one, change only the image
plus the three telemetry identity values (and any deliberate env addition), and
**prove it with a normalised diff** before registering. Run
`doctor aws-ecs --json` as a command override on the **candidate web task
definition** (not the separate doctor family — that one does not carry the
profile grants) and require exit 0; the doctor exits 1 on any failing check, so
exit 0 is a real all-clear. Note the web family leads with the
`cloudwatch-agent` sidecar, so `containers[0]` is the wrong container to grade.

Deploy zero-overlap: circuit breaker on, rollback off,
`minimumHealthyPercent=0`, `maximumPercent=100`, `desiredCount=1`. The
`services-stable` waiter proves nothing on its own — assert PRIMARY
`rolloutState=COMPLETED`, `failedTasks=0`, a single deployment, a healthy ALB
target, and `/api/health` + `/api/ready` both 200.

Epoch is 45 on both sides, so **no store recreation is expected**. If a doctor
reports `session_schema: stale`, stop and re-read the epoch — do not reflexively
`--init-schema`. If recreation genuinely is needed, it runs through the
`database-bootstrap` task definition (the runtime role has no CREATEDB), and
the stock `schema-init-doctor` pins a **stale image** that will write the wrong
epoch — re-register it on the candidate digest first.

## Part 3 — the battery

Corpus and verbatim intents: `round3-graph-corpus.md`. Driver:
`scripts/acceptance_battery.py` plus the harness in `ops-local/acceptance/`
(`drive_graph.py`, `extract_intents.py`, `run_task.py`, `analyse_compose.py`).

**Run composes strictly serially.** Round 3 ran six concurrently and lost two to
the 270s wall-clock. Note the nuance: the timeouts are **not purely load** — a
serial g08 also 422'd at 271s while the same graph composed in 238s under load.
Compose duration varied 43–272s for comparable graphs, and roughly 1 in 5 died
at the wall. Serial is still correct; just do not report a timeout as a defect
without a control.

Round-3 baseline to beat, all 11 graphs:

| | Graph | Round-3 outcome |
|---|---|---|
| ☀️ | g01 linear multi-transform | `completed` |
| ☀️ | g02 gate + routing + poisoned row | `completed_with_failures` 4/1/1 (designed) |
| ☀️ | g03 fork / coalesce | `completed` |
| ☀️ | g04 json + explode | `completed` |
| ⛈ | g05 text → text | **failed** — `elspeth-cfcd333f83` |
| ☀️ | g06 sink variety | `completed` |
| ☀️ | g07 Textract profile-first | `completed_with_failures` 1/1 (designed) |
| ⛅ | g08 row_union A/B ×4 | 1 failed, 1 cwf, 1 completed, 1 compose-422 |
| ☀️ | g09 four LLM nodes | `completed` 15/18 |
| ☀️ | g10 LLM → fixed mapper | `completed` |
| ⛈ | g11 llm source | **failed** — `elspeth-39118dd24f` |

### Confirm targets

| Ticket | P | State | What must be true live |
|---|---|---|---|
| `elspeth-902fc354b2` | P2 | `verifying` | **Primary.** g08 composes and runs clean; no `extra_forbidden` at a downstream fixed-schema consumer. Sample **×3** — round 3 measured this shape intermittent at 1-in-4 |
| `elspeth-47fa7c01eb` | P1 | `verifying` | A run with zero succeeded tokens must be *readable*: `/runs/{id}`, `/diagnostics`, `/outputs`, `/results` and `/api/sessions/{id}/runs` all non-500. Construct one deliberately |
| `elspeth-a79f1b2e6b` | P1 | closed | Cost holds at ~USD 0.42/session across a **full** battery, not just the trial sample |
| `elspeth-9d13900064` | P2 | `verifying` | Reproduced on its own deployed fix in round 3 — **must not advance to closed** until its own shape passes end to end |

### Still open, re-confirm as live

`elspeth-39118dd24f` (P1, triage — g11), `elspeth-cfcd333f83` (P2, triage —
g05), `elspeth-82d4c5146c` (P2, triage — fires on every runtime
`PluginContractViolation`). Each has a known reproducing shape; add a dated
datum, do not file duplicates.

### Not sampleable through the API

`elspeth-49b467d91a` (frontend DOM state — needs Playwright, not the driver)
and `elspeth-454892147c` (needs an induced retryable provider failure; no
fault-injection lever exists on this surface). Report unsampled rather than
faking either.

## Part 4 — measure cost, not just wall-clock

This is new for round 4 and is **not optional**: the cache fix and the reasoning
knob both move cost and latency in opposite directions, so wall-clock alone can
hide a regression.

The session store has **four** roles — `tool`, `audit`, `assistant`, `user` —
and the messages API projects **only** `user` and `assistant`. Every cost fact
is in the `audit` rows. Query read-only through the `database-bootstrap` task
definition (`ops-local/acceptance/make_inspect_override.py` is the working
pattern; it sets the transaction read-only):

```sql
SELECT count(*)                                                    AS llm_calls,
       sum((content::jsonb ->> 'total_tokens')::bigint)            AS total_tokens,
       round(sum((content::jsonb ->> 'provider_cost')::numeric),2) AS usd,
       count(DISTINCT session_id)                                  AS sessions
FROM chat_messages
WHERE role = 'audit' AND content LIKE '%llm_call_audit%';
```

Report **USD per session** and **tokens by call index**. Reference points:

| Measurement | Value |
|---|---|
| Round-3 baseline | USD **1.3394** / session; 168 calls, 10.9M tokens, USD 22.77 total |
| Post-cache-fix trial | USD **0.4246** / session |
| Round-3 fixed prefix | 49,091–49,645 tokens (±0.6% across 17 sessions) |
| Round-3 growth | 49.3k → 88.2k by call index 12 |

Note two corrections the addendum established, so round 4 does not re-derive
them: the cached region is **tools + system only** (~31k) — the dynamic context
and the whole message history are re-billed at full price — and the **schema
carry-forward contributed zero tokens** in round 3 (`_schemas_loaded_for_session`
is empty on a first compose). The growth is appended tool results, which stay in
the message array for the rest of the compose.

## Advisor — get Haiku out of this seat

**Operator position (2026-08-05):** Haiku is the wrong model for the advisor.
Stated preference, in order — *"I'd rather use Sonnet as primary and
zai/glm5.2 as the advisor than use Haiku."*

That preference is also the better engineering answer, not just a taste call.
The distinctness invariant exists because *a model checking its own work shares
its blind spots* (`config.py:945-952`). A second Anthropic model satisfies the
rule by string but only partly by intent; a different **vendor** satisfies it
properly.

### Hard constraints

- The advisor **must differ** from the composer. Enforced twice — app
  (`config.py:957`, canonical final-path-segment comparison) and terraform
  (`scenario-a/variables.tf:151`). `sonnet-4-6` is therefore unavailable while
  it is the composer.
- Terraform requires the advisor to be a **`bedrock/...`** id
  (`variables.tf:160`). Anything off Bedrock needs that validation relaxed plus
  a credential and VPC egress — a materially bigger change.
- The task role may only invoke **explicitly granted** model ARNs. The module
  comment at `locals.tf:193-196` warns about exactly this trap: a profile
  naming a third model passes startup validation and then dies at invoke with
  AccessDenied. Grant before you deploy.
- `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` is in
  `_TASK_DEFINITION_COMPOSER_MODEL_ENV`, a **protected** name the acceptance
  verifier checks against the scenario inventory (`task_definition.py:344`).
  Changing it drifts that binding and fails `verify_task_definition` until the
  inventory is updated. **The ordinary doctor does not check this** — a green
  doctor is not evidence the change is clean.
- Cost is not the argument either way. The Haiku advisor was **USD 0.36 of
  22.77** (1.6%). This is a quality decision.

### Options, best first

**Option A — `bedrock/zai.glm-…` (matches the operator's preference).**
The plumbing already exists: `locals.tf:190` carries a `zai.` prefix in the
grant-derivation list, and `terraform/README.md:700` gives a worked example
pairing `zai.glm-4.7-flash` with an Anthropic Sonnet composer. Different vendor,
so genuine failure-mode independence, and it stays on Bedrock so no credential
or egress change.
**Unresolved:** whether Bedrock offers a Z.ai model in `ap-southeast-1` for this
account. Verified 2026-08-05 that there is **no `zai`/`glm` inference profile**
in-region (21 profiles listed, none matching). Whether a *foundation model* is
offered could not be checked — `bedrock:ListFoundationModels` is denied to
`elspeth-installer`, and the admin session had expired.
**Settle it first** with `aws bedrock list-foundation-models --region
ap-southeast-1 --query "modelSummaries[?contains(modelId,'zai')]"` under the
admin profile (operator may need `aws login`), and check model access is
enabled. If nothing is offered in-region, Option A is dead here regardless of
merit — say so rather than substituting silently.

**Option B — `bedrock/global.anthropic.claude-sonnet-5`.**
Ready to go: the IAM grant **is already applied** to the task role (three ARNs
added 2026-08-05, profile `ACTIVE`), so only the env flip and a redeploy remain.
A different generation from the `sonnet-4-6` composer, so more independent than
`sonnet-4-5` and far more capable than Haiku — but same vendor, so weaker on the
invariant's intent than Option A.

**Option C — relax the distinctness invariant and run `sonnet-4-6` as both.**
Cheapest mechanically, and the operator's phrasing does not rule it out. But it
deletes a guard enforced in two places whose rationale is explicit, and makes
the advisor the composer reviewing itself. **Do not take this without an
explicit operator decision on the record.**

Whichever is chosen, update the terraform variable default as well as the task
definition — otherwise the next cold install silently reverts to Haiku.

## Reporting

One report at `docs/acceptance/2026-08-05-battery-round-4-report.md` mirroring
round 3: per-graph verdict table **with run ids**, the cost table, rider ratios,
and new defects labelled `battery-2026-08-XX`. A tracker timeline entry and an
**AWS ledger row for every mutation**. Evidence comments on tickets the round
discharges.

**Recommend closes; do not close.** Operator sign-off closes, and the close then
carries `close_commit release/0.7.2@<tip>`. Stochastic items never close on a
single pass.

## Five things previous rounds learned the hard way

1. **Reproduce before believing a diagnosis — including your own.** Round 3's
   first Textract run looked like a Textract failure; it died at its source node
   and never reached the transform. Re-driving produced the opposite conclusion.
2. **A mechanism you can read is not a mechanism that fired.** The round-3 cost
   brief blamed the schema carry-forward from its docstring. Direct measurement
   showed it contributed **zero** tokens. Check that the code ran, not just that
   it exists.
3. **`verifying` is not `closed`, and the battery *is* the verification.** When
   a battery reproduces a `verifying` ticket's symptom, file the sibling **and**
   record a negative datum; do not let the original advance.
4. **Prove a negative is discriminating before you rest on it.** Round 3's
   custody-NFR search returned zero in both the fixed and unfixed worlds, so it
   proved nothing and was retracted. Ask: *would this test have failed before
   the fix?*
5. **A live repro expires.** Capture listings and JSON into the ticket while the
   instance is up. Round 3's store recreation destroyed the stranded session two
   P1s named as their repro path.
