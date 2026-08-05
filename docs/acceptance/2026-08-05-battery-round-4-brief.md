# Brief: tear down Singapore, cold-install Sydney, run battery round 4

Date written: 2026-08-05. Author: `claude-r3-deploy`. Audience: the session that
does it. Supersedes the earlier Singapore-redeploy framing of this file.

Read first:

1. `docs/runbooks/aws-ecs-cold-install.md` — the 9-step install and the
   teardown. **This is the runbook**, not the 3,764-line
   `aws-ecs-deployment.md` program.
2. `2026-08-05-battery-round-3-report.md` — per-graph results and the defect
   list.
3. `2026-08-05-compose-token-cost-brief.md` **and its addendum** — cost. Where
   they disagree, the addendum is right.

## Why

The acceptance stack is in `ap-southeast-1` (Singapore). It should be in
`ap-southeast-2` (Sydney). Sydney also carries the models this needs:

| | Singapore | Sydney |
|---|---|---|
| Providers | 4 (Amazon, Anthropic, Cohere, TwelveLabs) | 13, incl. **Z.AI**, OpenAI, Google, Qwen, DeepSeek, Moonshot |
| Z.ai GLM | absent | `zai.glm-5`, `zai.glm-4.7`, `zai.glm-4.7-flash` |
| `au.` residency profiles | absent | `au.anthropic.claude-sonnet-4-6`, `au.anthropic.claude-sonnet-5`, … |

**Haiku as advisor is an operator-declared blocker** — it generates bad
advisory data. Sydney fixes that properly: GLM-5 is a different vendor, so it
satisfies the distinctness invariant (`config.py:957`) in intent, not just in
string.

`aws_region` now defaults to `ap-southeast-2` in all three terraform modules
(`ea7d70d6f`). It stays overridable — this is an open-source package.

## Build under root

**Use the root profile `elspeth-acceptance` for the whole install.** Operator
decision, and it is the only path that works: `elspeth-e54343d43b` (P1, open)
records that the shipped installer IAM policies **cannot complete a create** —
the R3 apply failed at 38 of 86 resources under `elspeth-installer`, and 47 of
the module's resources have never been created by a condition-gated principal.
Singapore itself was only finished by switching both provider aliases to root.

So: **skip runbook step 2** (render/attach the narrow installer policies). That
step is the thing that is broken, and this is a disposable environment. Do not
spend the round trying to fix it — that is `elspeth-e54343d43b`'s job, and the
tracker holds that workstream.

Root sessions expire. Refresh with `aws login --profile elspeth-acceptance`
before starting, and expect to refresh mid-run.

## Part 1 — tear down Singapore

Use the **same checkout, tfvars, backend, workspace, profiles, account and
Region** that installed it. Order is Scenario A, then the state census, then
bootstrap. From `docs/runbooks/aws-ecs-cold-install.md` §Teardown:

1. `terraform -chdir=scenario-a plan -destroy` → `show` → `apply`; then assert
   `terraform -chdir=scenario-a state list` is **empty**.
2. **Census every state object in the backend bucket** before touching
   bootstrap — the bucket is shared across scenarios and workspaces, so an
   empty Scenario A proves nothing about Scenario B. Each object must have
   `(.resources // []) | length == 0`.
3. Empty the bucket explicitly — **object versions and delete markers both**.
   The backend deliberately carries no `force_destroy`, so bootstrap destroy
   refuses a non-empty bucket. That is a guard, not an obstacle; do not add
   `force_destroy` to get past it.
4. Destroy bootstrap.
5. Run the terminal `orphan-sweep` across ECS, ALB, Aurora, EFS, Secrets
   Manager, CloudWatch Logs, EventBridge, Cognito, ECR and Guardrails.

**Keep Route53, the hosted zone, and NS records.** They survive every teardown.
The **ACM certificate does not transfer** — ALB certs are region-bound, so
Sydney needs its own cert in `ap-southeast-2`, and the Singapore one goes with
the Singapore stack.

**Before destroying anything, extract what round 3 and 4 need**: the session
store holds the cost baseline and every compose transcript. If you want
before/after cost comparison, run the read-only aggregate (Part 4) and save the
output **first** — teardown destroys it. Round 3's own lesson: a live repro
expires.

Known non-closing surface: a successful destroy leaves the Container Insights
performance log group behind. ECS's service-linked role re-creates it minutes
after the cluster goes INACTIVE, untagged and outside every terraform state, so
the tag-based orphan sweep cannot see it. `cleanup_container_insights_log_group`
in `aws-ecs-deployment.md` handles it — run it last.

## Part 2 — cold-install Sydney

`docs/runbooks/aws-ecs-cold-install.md`, steps 1 and 3–9 (2 is skipped, above).
New `ACCEPTANCE_RUN_ID`; tag every resource with it.

**Models.** Composer and advisor must differ (`config.py:957`, canonical final
path segment; terraform `variables.tf:151`).

- Composer: **`bedrock/au.anthropic.claude-sonnet-4-6`** — the `au.` profile
  keeps inference in-country.
- Advisor: **`bedrock/zai.glm-5`**.

**The advisor is a foundation model, not an inference profile.** There is no
`zai` inference profile in any region — verified. So the task-role grant needs
the **foundation-model ARN** form (`arn:aws:bedrock:ap-southeast-2::foundation-model/zai.glm-5`
and the `arn:aws:bedrock:*::foundation-model/…` sibling), not an
`inference-profile/…` ARN. `locals.tf:190` already carries a `zai.` prefix in
the grant-derivation list, and `terraform/README.md:700` shows the intended
pairing. The module comment at `locals.tf:193-196` warns about the exact trap:
a model that passes startup validation and then dies at invoke with
AccessDenied. **Grant before you deploy, and confirm model access is enabled
for Z.AI in the Bedrock console** — a new provider usually needs it.

**Images.** ECR is per-region, so both images (`elspeth-web`,
`elspeth-cloudwatch-agent`) must be rebuilt or re-pushed into the Sydney
registry and deployed **by digest**. Build `linux/amd64`, extras
`webui llm aws postgres`. Verify before pushing: revision label matches the
deployed SHA, uid/gid `1654`, `--version`, and a container smoke asserting the
frontend dist is present and world-readable, `boto3` imports, `psycopg` **and**
`psycopg2` import, and `SESSION_SCHEMA_EPOCH`.

**Watch item:** `elspeth-9f7d336e1c` (P1, `fixing`) — *"release/0.7.2 Terraform
package cannot boot any published image."* If the doctor or the service will
not start on a freshly published image, that is this defect. Stop and report;
do not improvise around it.

**Scan gate:** buildx publishes an OCI index, and ECR registers findings against
the **platform child manifest**. `describe-image-scan-findings` on the index
digest returns `ScanNotFoundException` forever. Resolve the `linux/amd64` child
from `docker buildx imagetools inspect --raw` and scan that. Verify the chain —
task-definition pin == running digest, index's amd64 child == scanned digest —
rather than the runbook's `RUNNING_DIGEST = SCAN_DIGEST`, which is
unsatisfiable for a buildx push.

**Doctors.** Schema-owner doctor, then runtime-credential doctor, then enable
one task. Run `doctor aws-ecs --json` as a command override on the **candidate
web task definition** — the separate doctor family does not carry the profile
grants. Exit 0 is a real all-clear (the doctor exits 1 on any failing check).
The web family leads with the `cloudwatch-agent` sidecar, so `containers[0]` is
the wrong container to grade.

**Env that must be present** (all `ELSPETH_WEB__`; new settings are atomic with
the image that knows them — `config.py:1134` refuses unknown prefixed vars and
crashes the container at boot):

- `AWS_TEXTRACT_PROFILES` — ADR-036 grant, alias `acceptance-docs` → app bucket
  + org prefix. Without it the Textract transform is honestly unauthorable.
- `AWS_S3_SOURCE_PROFILES` — same bucket/prefix, `prefix` not `key_prefix`.
- `COMPOSER_CANDIDATE_REASONING_EFFORT` — carry `medium`; it unblocked g08.
- `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1`.

**Upload a document** to the granted prefix before the Textract graph runs;
Singapore had `docs/exec-summary.pdf`. The g07 intent also names a
deliberately-absent key to exercise the quarantine arm — keep it absent.

## Part 3 — battery round 4

Corpus and verbatim intents: `round3-graph-corpus.md`. Driver:
`scripts/acceptance_battery.py` plus `ops-local/acceptance/` (`drive_graph.py`,
`extract_intents.py`, `run_task.py`, `analyse_compose.py`,
`make_inspect_override.py`). Those carry Singapore identifiers — update the
cluster, subnets, security group and base URL for Sydney.

**Run composes serially.** Round 3 ran six concurrently and lost two to the
270s wall-clock. The nuance: timeouts are not purely load — a serial g08 also
422'd at 271s while the same graph composed in 238s under load. Roughly 1 in 5
composes died at the wall. Serial is right; just do not report a timeout as a
defect without a control.

Round-3 baseline to beat:

| | Graph | Round 3 |
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

Confirm targets — check live status first, several moved during round 3:

| Ticket | What must be true live |
|---|---|
| `elspeth-902fc354b2` | g08 composes and runs clean; no `extra_forbidden` at a downstream fixed-schema consumer. **Sample ×3** — measured intermittent at 1-in-4 |
| `elspeth-47fa7c01eb` | A run with zero succeeded tokens must be readable: `/runs/{id}`, `/diagnostics`, `/outputs`, `/results`, `/api/sessions/{id}/runs` all non-500. Construct one deliberately |
| `elspeth-a79f1b2e6b` | Cost holds at ~USD 0.42/session across a **full** battery, not the trial sample |
| `elspeth-9d13900064` | Reproduced on its own deployed fix in round 3 — **must not advance to closed** until its own shape passes end to end |
| `elspeth-cd0f6a6cd9` | ADR-036 re-confirm on the new stack: authored node carries `profile` + relative `key_field` and **no** bucket/region/prefix; the miss fails with `submit_failed`, **not** `bucket_region_unverified` |

Still open, re-confirm as live: `elspeth-39118dd24f` (g11), `elspeth-cfcd333f83`
(g05), `elspeth-82d4c5146c`.

Not sampleable through the API: `elspeth-49b467d91a` (frontend DOM — needs
Playwright), `elspeth-454892147c` (needs induced provider failure; no lever
exists). Report unsampled rather than faking either.

**New for Sydney:** GLM-5 as advisor is itself under test. Watch the advisor
FLAG rate — Singapore ran 14 of 16 `verdict=flagged` at `phase=early`, which
made repair the normal path. Record the Sydney rate; a large change either way
is a finding.

## Part 4 — measure cost

Mandatory. The cache fix and the reasoning knob move cost and latency in
opposite directions, so wall-clock alone hides regressions.

`chat_messages` has **four** roles — `tool`, `audit`, `assistant`, `user` — and
the messages API projects **only** `user` and `assistant`. Cost lives in the
`audit` rows. Query read-only through the `database-bootstrap` task definition
(`ops-local/acceptance/make_inspect_override.py` sets the transaction
read-only):

```sql
SELECT count(*)                                                    AS llm_calls,
       sum((content::jsonb ->> 'total_tokens')::bigint)            AS total_tokens,
       round(sum((content::jsonb ->> 'provider_cost')::numeric),2) AS usd,
       count(DISTINCT session_id)                                  AS sessions
FROM chat_messages
WHERE role = 'audit' AND content LIKE '%llm_call_audit%';
```

Report **USD per session** and **tokens by call index**. Reference points:

| | |
|---|---|
| Round-3 baseline | USD **1.3394**/session; 168 calls, 10.9M tokens, USD 22.77 |
| Post-cache-fix trial | USD **0.4246**/session |
| Round-3 fixed prefix | 49,091–49,645 tokens (±0.6% across 17 sessions) |

Two corrections already established — do not re-derive: the cached region is
**tools + system only** (~31k); the dynamic context and whole message history
are re-billed at full price. And the **schema carry-forward contributed zero
tokens** in round 3 (`_schemas_loaded_for_session` is empty on a first
compose) — the growth is appended tool results.

GLM-5 pricing differs from Haiku's, so the advisor line will move. Report it
separately from the composer line rather than letting it muddy the per-session
figure.

## Reporting

`docs/acceptance/2026-08-0X-battery-round-4-report.md`: per-graph verdict table
**with run ids**, the cost table, rider ratios, new defects labelled
`battery-2026-08-XX`. Tracker timeline entry and an **AWS ledger row for every
mutation** — teardown included.

**Recommend closes; do not close.** Operator sign-off closes, and the close
carries `close_commit release/0.7.2@<tip>`. Stochastic items never close on a
single pass.

## Five things earlier rounds learned the hard way

1. **Reproduce before believing a diagnosis — including your own.** Round 3's
   first Textract run looked like a Textract failure; it died at its source
   node and never reached the transform.
2. **A mechanism you can read is not a mechanism that fired.** The cost brief
   blamed the schema carry-forward from its docstring; measurement showed it
   contributed zero.
3. **`verifying` is not `closed`, and the battery *is* the verification.** When
   a battery reproduces a `verifying` ticket's symptom, file the sibling **and**
   record a negative datum.
4. **Prove a negative is discriminating before resting on it.** Round 3's
   custody-NFR search returned zero in both the fixed and unfixed worlds. Ask:
   would this test have failed before the fix?
5. **A live repro expires.** Capture listings and JSON into the ticket while the
   instance is up — and here, before teardown.
