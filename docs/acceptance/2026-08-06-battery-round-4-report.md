# Battery round 4 — tear down Singapore, cold-install Sydney

Status: **complete**. Run 2026-08-06 (UTC 2026-08-05T18:40–20:00). Brief:
[`2026-08-05-battery-round-4-brief.md`](2026-08-05-battery-round-4-brief.md).

Branch `release/0.7.2` @ `071501783`.

---

## Part 0 — pre-teardown capture (Singapore, `ap-southeast-1`)

Run `cf548430-ae8b-48bc-bcc2-9dcf1b57a10e`. The brief requires the cost
baseline to be extracted before teardown destroys the session store. It was,
plus the run inventory and the live task-definition environment, which round 4
needs for parity. Raw output is preserved under
`ops-local/acceptance/r3-preserve/`.

### Correction 1 — the audit payload does not carry the columns the brief's query assumes

The brief's cost query selects `total_tokens` and `provider_cost` (both
present) but the surrounding analysis assumes `model`, `prompt_tokens`, and
cache columns. Probed key set of an `llm_call_audit` row, live:

```
provider_cost  model_requested  model_returned  total_tokens
reasoning_tokens  status  _kind
```

There is no `model`, no `prompt_tokens`, and **no cached-token columns at
all**. Consequences:

- A model split must key on `model_requested`, not `model`. Keying on `model`
  returns a single `null` bucket — which reads like "one model" rather than
  like a failed query.
- The brief's established correction that "the cached region is tools + system
  only (~31k)" is **not verifiable from this store**. Whatever produced that
  figure, it was not this table. It is carried forward as an unverified claim,
  not as an established one.

### Correction 2 — the round-3 baseline figures are close but not the ones in the brief

The store is cumulative and had grown since the brief was written. Splitting by
session set — the cohort discriminator; key presence is not — gives:

| Cohort | Sessions | Calls | Tokens | Total USD | Mean USD/session | Median | Min | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-04 (pre-cache-fix) | 18 | 172 | 11,085,834 | 23.25 | **1.2915** | 1.1159 | 0.4776 | 3.0940 |
| 2026-08-05 (post-cache-fix) | 12 | 98 | 5,675,313 | 5.93 | **0.4946** | 0.4806 | 0.1901 | 0.7442 |
| Whole store | 30 | 270 | 16,761,147 | 29.18 | 0.9727 | 0.7666 | 0.1901 | 3.0940 |

Against the brief's reference points:

| | Brief | Measured 2026-08-06 |
|---|---|---|
| Round-3 baseline | USD 1.3394/session; 168 calls, 10.9M tokens, USD 22.77 | USD **1.2915**/session; 172 calls, 11.09M tokens, USD 23.25 |
| Post-cache-fix | USD 0.4246/session | USD **0.4946**/session (mean), 0.4806 (median) |

The baseline reconciles. The post-fix figure does **not**: the fuller 12-session
cohort sits ~16% above the trial sample's 0.4246. This bears directly on
`elspeth-a79f1b2e6b` ("cost holds at ~USD 0.42/session across a **full**
battery") — on round-3 evidence it holds at ~0.49, and the 0.42 figure was a
small-sample artefact. Round 4 measures this properly.

### Correction 3 — the advisor is ~2.5% of cost, and runs exactly once per session

| Model | Calls | Tokens | USD | Sessions |
|---|---:|---:|---:|---:|
| `bedrock/global.anthropic.claude-sonnet-4-6` (composer) | 242 | 16,142,993 | 28.4600 | 30 |
| `bedrock/global.anthropic.claude-haiku-4-5…` (advisor) | 28 | 618,154 | 0.7222 | 28 |

`advisor_calls_per_session` is `1` for all 28 sessions — the advisor is a
single END-gate consultation, not a per-repair-round loop. At ~USD 0.026 per
session it is **2.5% of the per-session figure**, so swapping Haiku for GLM-5
cannot materially move the headline cost either way. The GLM-5 change should be
judged on advisory *quality* (the operator-declared blocker), not on cost.

### Round-3 composer token growth, by cohort

Composer calls only (the earlier per-session index mixed the single ~19k
advisor call in at an arbitrary position and flattened the curve):

| Call index | 08-04 avg | 08-04 min | 08-05 avg | 08-05 min |
|---:|---:|---:|---:|---:|
| 1 | 68,076 | 49,294 | 65,408 | 46,705 |
| 2 | 68,135 | 49,143 | 62,952 | 38,213 |
| 3 | 69,548 | 49,093 | 62,541 | 45,593 |
| 4 | 64,640 | 49,118 | 60,705 | 45,594 |
| 5 | 73,131 | 57,651 | 66,096 | 46,258 |
| 6 | 71,902 | 49,091 | 71,997 | 53,058 |
| 8 | 75,126 | 57,863 | 67,982 | 52,496 |
| 10 | 81,383 | 70,720 | 67,600 | 58,112 |
| 12 | 78,004 | 66,386 | 57,329 | 57,329 |

The 08-04 minima (49,091–49,321) reproduce the brief's stated fixed prefix of
49,091–49,645 exactly, so that figure is confirmed. Post-fix the prefix falls
to ~38k–46k and the curve flattens: 08-04 climbs ~15% from call 1 to call 12,
08-05 does not climb at all. Cost therefore scales with **call count**, not
with per-call context growth.

### Call outcomes

| Status | Calls | Missing token accounting |
|---|---:|---:|
| `success` | 262 | 0 |
| `timeout` | 8 | 8 |

All 8 timeouts are composer calls with `model_returned: null`. `reasoning_tokens`
totals 34,791 across all successful calls. No silent model substitution:
`model_requested` and `model_returned` agree on every non-timeout call.

### Round-3 run inventory (preserved — unrecoverable after teardown)

16 runs. Run ids retained for the round-4 comparison table.

| Run id | Status | proc | ok | fail | quar |
|---|---|---:|---:|---:|---:|
| `0f6f9db0-3aa3-4ef0-ac2c-5c153aa2fe64` | completed | 8 | 8 | 0 | 0 |
| `307c4e5c-0e5b-48b2-a64a-7142431e2c77` | completed | 3 | 3 | 0 | 0 |
| `37caf1ee-00c2-48af-b3ca-2f5d39963154` | completed | 5 | 5 | 0 | 0 |
| `57cc5039-0567-4edf-a4df-dee0c912c15e` | completed | 6 | 6 | 0 | 0 |
| `5c9964cb-974c-4f24-9198-45e087d4e47b` | completed | 5 | 15 | 0 | 0 |
| `67d40936-2261-4b30-9b43-7058f7927e53` | completed_with_failures | 2 | 1 | 1 | 0 |
| `7c429b4f-d1ab-465d-ae92-72e845cc68e6` | completed_with_failures | 8 | 8 | 8 | 0 |
| `a4f534df-2319-46be-95e0-453028375c13` | failed | 0 | 0 | 0 | 0 |
| `adf0b6c6-bdcb-4e29-ba23-b953bae5366c` | failed | 0 | 0 | 0 | 0 |
| `b944ac9c-52a1-421b-8ed2-93b8b982cfdf` | completed | 3 | 3 | 0 | 0 |
| `bb683d20-bf9a-4c47-bc91-d9b0bb7bbcbf` | completed_with_failures | 5 | 4 | 1 | 1 |
| `c3e36a91-fa65-4b4e-81eb-41bc260123e0` | completed | 3 | 6 | 0 | 0 |
| `c69b6ab6-e462-46d8-bd9b-2c8a811bb02f` | completed_with_failures | 2 | 0 | 2 | 2 |
| `da440bae-d960-4af3-a913-0603bbb2afc7` | completed | 4 | 4 | 0 | 0 |
| `dc689cfe-9f4b-47ba-bd04-abd04309debc` | failed | 0 | 0 | 0 | 0 |
| `ed3b37c9-4fbf-45af-8cfd-d97ffd685390` | failed | 0 | 0 | 0 | 0 |

Two rows worth carrying into round 4:

- `7c429b4f` reports 8 processed, 8 succeeded **and** 8 failed. Succeeded plus
  failed exceeds processed, which is the accounting shape `elspeth-47fa7c01eb`
  concerns.
- `c69b6ab6` is a **zero-succeeded-token run** that already exists in round 3
  (2 processed, 0 succeeded, 2 failed, 2 quarantined). `elspeth-47fa7c01eb`
  asks for one to be constructed deliberately; round 3 produced one incidentally.

---

## Part 0b — two landmines found before teardown

### The local tfvars were pre-flipped to Sydney while the live stack was in Singapore

`examples/scenario-a.tfvars:7` and `examples/bootstrap.tfvars:3` both read
`aws_region = "ap-southeast-2"` (edited 2026-08-06 03:50), while the live
stack, its backend bucket, its ECR digests and its IAM boundary are all
`ap-southeast-1`. Both files are gitignored, so nothing tracked showed the
divergence.

A destroy in that state would have refreshed every regional resource against an
empty Region, recorded them as already-gone, and emptied the state — while
**IAM is global**, so the task roles, execution role and permissions boundary
would have been genuinely deleted. Outcome: a live, billing, un-IAM'd Singapore
stack that Terraform no longer knows exists.

Corrected before any plan was generated. Exactly two lines were wrong; every
other region-bearing value (images, Bedrock ARNs) still read `ap-southeast-1`
and was correct. Verified by `grep -n 'ap-southeast'` over both files rather
than by re-reading the line that was changed.

Gate used before the destroy apply: `terraform state list | wc -l` — a count
taken from the backend, independent of the provider region — must equal the
destroy plan's "N to destroy".

### A clean cold install would boot with the wrong plugin allowlist

The live web task definition (`a-fa1b99c60192978b10f7-web:15`) carries a
**40-entry** `ELSPETH_WEB__PLUGIN_ALLOWLIST`. Terraform's `resolved_inventory`
carries **15**. Round 3's expanded share was applied outside Terraform through
`ops-local/acceptance/build_td.py`, so it lives in no tracked input.

A Sydney cold install from the package alone would therefore boot with the
15-entry list, and `json_explode`, the `batch_*` family, `blob_fetch`,
`value_transform`, `truncate`, `type_coerce`, `keyword_filter`,
`line_explode`, `report_assemble`, `rag_retrieval`, `sink:database` and
`sink:chroma_sink` would be unauthorable at compose time. Most of the round-3
corpus would fail to compose, and it would read as a composer regression rather
than as a configuration gap.

The full 40-entry list is preserved in
`ops-local/acceptance/r3-preserve/sg-web-taskdef-live.json` and is set in the
Sydney tfvars.

### Other live values that must be carried, not defaulted

| Setting | Live (web:15) | Package default | Note |
|---|---|---|---|
| `COMPOSER_TIMEOUT_SECONDS` | **270** | 240 (`locals.tf:391`) | Round-3 composes died at 271s; defaulting silently removes 30s |
| `COMPOSER_CANDIDATE_REASONING_EFFORT` | `medium` | unset | Unblocked g08 |
| `COMPOSER_MAX_DISCOVERY_TURNS` | 8 | — | |
| `COMPOSER_MAX_COMPOSITION_TURNS` | 12 | — | |
| `COMPOSER_RATE_LIMIT_PER_MINUTE` | 30 | — | |
| `COMPOSER_BOOT_PROBE_ENABLED` | `true` | — | |
| `ELSPETH_PLANNER_REJECTION_DETAIL_LOG` | 1 | unset | |

Workstation IP re-checked for `alb_https_ingress_cidrs`: `202.153.215.203`,
unchanged from the round-3 install.

### Watch item status, checked before burning the bridge

`elspeth-9f7d336e1c` ("release/0.7.2 Terraform package cannot boot any
published image", P1) is at **`fixing`**, assigned to the operator, blocked by
`elspeth-97d0c15eb6` — i.e. still live. The brief orders the teardown and the
environment is disposable, so this proceeds, but it is recorded here as the
known risk taken: Singapore is destroyed before a Sydney image is proven to
boot, and there is no fallback stack. The live Singapore image
(`sha256:a2b483f8…`, release `f65af925806a`) is the last known-booting build.

---

## Part 1 — teardown of Singapore

Complete. `ap-southeast-1`, run `cf548430-ae8b-48bc-bcc2-9dcf1b57a10e`, root
profile `elspeth-acceptance` on both provider aliases.

| Step | Result |
|---|---|
| Scenario A destroy | **86 destroyed**, 0 added, 0 changed; `state list` empty |
| Backend census | 1 state object (`elspeth/scenario-a/terraform.tfstate`), tracks 0 resources; **no Scenario B object existed** |
| Bucket emptied | 28 versions + delete markers, in one batch; 0 remaining |
| Bootstrap destroy | **10 destroyed**; `state list` empty |
| Orphan sweep | 0 clusters, 0 ALBs, 0 target groups, 0 RDS, 0 EFS, 0 ECR, 0 S3 (account-wide), 0 secrets, 0 EventBridge rules, 0 Cognito pools, 0 guardrails, 0 non-default VPCs |
| Terminal tag query | Non-TD hits all reconciled as tombstones — cluster `INACTIVE`, all 5 security-group rules `GONE` |
| Task definitions | 12 out-of-band ACTIVE revisions deregistered (see below) |
| Container Insights | cleanup running with the runbook's 600s quiet window |

Both destroy plans were gated on set equality against `terraform state list`
before apply, not on a raw count:

| | State list | Planned deletes | Gap |
|---|---:|---:|---|
| scenario-a | 91 | 86 | 5 data sources (`aws_availability_zones`, 4× `aws_iam_policy_document`) |
| bootstrap | 11 | 10 | 1 data source (`ecs_permissions_boundary`) |

Data sources appear in `state list` and are never destroyed, so the gap is
expected; what mattered is that the *managed* sets were identical in both
directions. 86 also reconciles with `elspeth-e54343d43b`'s "38 of 86 resources".

**Route53, the hosted zone and NS records were not touched.** The two
`ap-southeast-1` ACM certificates (`elspeth-aws.foundryside.dev`,
`elspeth.aws.foundryside.dev`, both `ISSUED`, both `InUse: false`) were
**deliberately retained**. They are region-bound and therefore useless to
Sydney, but they are free, they were not Terraform-managed, and deleting them
would force DNS re-validation if Singapore is ever rebuilt. Certificates sit on
the DNS seam, which survives teardown.

### Finding — teardown does not reach task definitions registered outside Terraform

Terraform destroyed the task-definition revisions it owned (`…-web:6`,
`…-runtime-doctor:6`, `…-schema-init-doctor:6` and earlier). It never saw the
revisions `ops-local/acceptance/build_td.py` and `respin_td.py` registered
during the round: `…-web:7`–`:15`, `…-runtime-doctor:7`,
`…-schema-init-doctor:7`–`:8`. Twelve ACTIVE revisions survived a
"complete" destroy, all carrying `ACCEPTANCE_RUN_ID`.

Deregistered here. Task definitions are not chargeable, so this is a hygiene
and evidence-accuracy defect rather than a cost leak — but the runbook's
completion criterion reads as though the tag query proves the account is clean,
and it does not cover anything registered out of band.

The same sweep found **an entire prior namespace still ACTIVE**:
`a-4cb186732570bf935456-web:3`–`:52` (35 revisions), `…-doctor:4`–`:6`,
`…-database-bootstrap:4`. That is a different acceptance run, left behind by an
earlier teardown. It was **not** removed here — the runbook is explicit that a
resource must not be deleted merely because its name resembles this run, and
the region now holds no cluster or service that could reference it. It is
reported for the operator.

### Correction — the runbook's state census could not fail

`aws s3api get-object … /dev/stdout` emits two JSON documents: the object body,
then the CLI's own response metadata. `jq -e` takes its exit status from the
*last* value produced, so the runbook's

```
| jq -e '(.resources // []) | length == 0'
```

graded the metadata document, which has no `.resources`, always satisfied the
test, and passed unconditionally. Mutation-tested against a synthetic state
tracking three live resources: the original form returned **PASS**; the
corrected `jq -e -s '(.[0].resources // []) | length == 0'` returned FAIL on
that input and PASS on a genuinely empty state.

This is the gate that stands between a teardown and erasing a live Scenario B
state. Fixed in `docs/runbooks/aws-ecs-cold-install.md`, with the
both-directions discrimination check added inline so the next operator can
confirm it still bites. The real census was then re-run in the corrected form
before the bucket was emptied.

## Part 2 — cold install of Sydney

Run `700e19d5-7894-4087-9a04-25aca8047b26`, `ap-southeast-2`, root profile
`elspeth-acceptance` on both provider aliases. Runbook step 2 skipped per the
brief (`elspeth-e54343d43b`: the shipped installer policies cannot complete a
create).

### Models — every premise verified live, not taken from the brief

| Claim | How checked | Result |
|---|---|---|
| `zai.glm-5` exists in Sydney | `list-foundation-models` | present, `ACTIVE`, TEXT, streaming |
| **No** `zai` inference profile exists | `list-inference-profiles`, filtered on `zai`/`glm` | `[]` of 29 ACTIVE profiles — brief confirmed |
| `au.anthropic.claude-sonnet-4-6` available | `list-inference-profiles` | `ACTIVE` |
| Z.AI model access enabled | **live `bedrock-runtime converse`** | returned `ok`, 269 ms |
| Composer model invocable | live `converse` | returned `ok`, 881 ms |
| `fast` profile model invocable | live `converse` | returned `ok`, 18 tokens |

The brief warned that "a new provider usually needs" console model access
enabled. It did not — access was already live. Verified by invoking, not by
reading a console page.

`au.anthropic.claude-sonnet-4-6` resolves to foundation models in **two**
Regions (`ap-southeast-2` and `ap-southeast-4`), which is why the grant needs
the wildcard sibling form; the module derives that automatically because `au.`
is a geography prefix. `zai.` is a **provider** label in
`bedrock_known_provider_prefixes`, so it derives no cross-region grant and the
explicit foundation-model ARNs are load-bearing. The plan produced no
`bedrock_unclassified_model_ids` precondition failure, which is the module's
own confirmation that the pairing is grantable.

Distinctness (`config.py:957`): final path segments `au.anthropic.claude-sonnet-4-6`
vs `zai.glm-5` — distinct as strings and as vendors.

### Images

Built from a **detached worktree at `52ab3ec8b`**, not from the shared
checkout: a concurrent writer modified seven source files and added one test at
04:54–04:56, two minutes after this session's commit. An image whose contents
do not match its `CANDIDATE_SHA` label is the exact provenance defect
`elspeth-9f7d336e1c` exists to prevent, so the build took its context from a
clean tree at a known commit. The other session's work was neither committed,
reverted, nor built.

Pre-publish verification passed: revision label == deployed SHA, `linux/amd64`,
uid/gid `1654`, `boto3` + `psycopg` + `psycopg2` + `elspeth.web` import,
`--version`, frontend dist present and **world-readable** (2 × `index.html` —
the container runs as 1654, so a root-owned 0600 dist boots fine and serves
nothing), `SESSION_SCHEMA_EPOCH=45`.

| | Digest |
|---|---|
| `elspeth-web` index | `sha256:fabe0cf9…` |
| `elspeth-web` amd64 child | `sha256:d614f114…` |
| `elspeth-cloudwatch-agent` index | `sha256:5e95635d…` |
| `elspeth-cloudwatch-agent` amd64 child | `sha256:4e8cca34…` |

The scan-manifest trap reproduced exactly as the brief describes, and was
confirmed in **both** directions rather than assumed:
`describe-image-scan-findings` on the index digest returns
`ScanNotFoundException`; on the amd64 child it returns `COMPLETE`. Both
publishes produced `application/vnd.oci.image.index.v1+json` with an `amd64`
child and an attestation child, even though the runbook's flow is
`buildx --load` then `docker push`.

### Finding (P2) — the runbook's zero-finding scan gate is unsatisfiable for the agent image

`elspeth-web` amd64 child: **COMPLETE, zero findings.** Clean.

`elspeth-cloudwatch-agent` amd64 child: **COMPLETE, 33 findings — 5 CRITICAL,
16 HIGH, 9 MEDIUM, 3 LOW.** Runbook step 4's `require_clean_scan` requires a
total of zero, so this is a documented stop condition.

Every finding is an OS package in the base layer — `perl`, `openssl`, `glibc`,
`sqlite3` — and **all 33 report no fix available upstream**
(`fixed_in_version` absent on every one). Bumping the pinned base digest
therefore cannot clear the gate; it is currently unsatisfiable for any
Debian-derived base.

The cause is narrower than it looks, and the fix is concrete.
`cloudwatch-agent-image/Dockerfile` copies `/opt/aws` out of
`amazon/cloudwatch-agent` and then runs it **on a `python:3.13-slim`
runtime**. The entrypoint,
`/opt/aws/amazon-cloudwatch-agent/bin/start-amazon-cloudwatch-agent`, is an
**ELF binary** — the agent is Go. Nothing in the image uses the Python base;
it contributes no capability and every one of the 33 CVEs. The app image, whose
runtime is a busybox/venv root rather than a distro, scans clean.

Recommendation: rebase the sidecar on a minimal runtime (upstream
`amazon/cloudwatch-agent` itself, or a distroless static base) and re-gate.
Filed against the cold-install qualification, `elspeth-671a17d5c0`.

**Proceeded past this gate deliberately**, and it is recorded rather than
waived silently: the findings are unfixed-upstream OS packages, in a monitoring
sidecar, in a single-tenant disposable account whose ALB ingress is restricted
to one operator IP — and stopping would deliver no battery at all. That is an
operator decision to ratify or reverse, not a clean pass.

## Part 3 — battery round 4

Instance `https://a-9bf256b5c6305b89f30e-alb-209940822.ap-southeast-2.elb.amazonaws.com`,
task definition `a-9bf256b5c6305b89f30e-web:2`, composer
`au.anthropic.claude-sonnet-4-6`, advisor `zai.glm-5`, composer timeout 270s,
composes run **serially**.

Image under test is `52ab3ec8b`; round 3 ran `f65af925806a`, **35 source
commits earlier**. That gap is the most likely home for any regression below,
and it includes the `elspeth-cfcd333f83` collision-fix cluster (`b144d499b`,
`0337b79cc`, `f3e11c770`), `02a80da51`, `7a5d72d34`, and the
`elspeth-82d4c5146c` terminal-outcome work (`3cb883229`, `7df62193b`) — all of
which touch schema/edge validation.

### Per-graph results

| | Graph | Round 3 | Round 4 | Run id / failure |
|---|---|---|---|---|
| ⛈ | g01 linear multi-transform | `completed` | **`empty`** | `ad27eb80-a69d-45a3-99f4-c76e6b595e53` — 4/4 rows discarded at source validation |
| ⛈ | g02 gate + routing + poisoned row | `completed_with_failures` 4/1/1 | **validate fails** | `graph_structure`: `type_coerce` edge rejected |
| ☀️ | g03 fork / coalesce | `completed` | `completed` | `4435838c-283a-490a-ba42-7f96a813c855` |
| ☀️ | g04 json + explode | `completed` | `completed` | `cb8fadc0-141b-4c43-9b13-d536da33102e` |
| ⛈ | g05 text → text | **failed** | **compose 200, no state** | `state_exists`: no composition state after a 200 in 53s |
| ☀️ | g06 sink variety | `completed` | `completed` | `32fe5f14-35d8-403b-b9af-1a0b365835ab` |
| ☀️ | g07 Textract profile-first | `completed_with_failures` 1/1 | `completed_with_failures` **1/1 (designed)** | `a8c05560-7f12-4526-8ffb-6a9089e4f8eb` |
| ☀️ | g08 row_union sample 1 | — | `completed` 8 ok / 0 failed | `a294a085-4f5e-42cf-8acc-a678fddf5546` |
| ☀️ | g08 row_union sample 2 | — | `completed` 8 ok / 0 failed | `56ba2cd9-dd0a-4f49-a3f8-0803cfd65ce7` |
| ⛅ | g08 row_union sample 3 | 1 of 4 compose-422 | **compose 422 @ 271s** | wall clock, not a defect |
| ⛅ | g09 four LLM nodes | `completed` 15/18 | **compose 422 @ 270s** | wall clock, not a defect |
| ☀️ | g10 LLM → fixed mapper | `completed` | `completed` | `e078afb9-2937-4693-a9d2-a6f05b15a59e` |
| ⛅ | g11 llm source | **failed** | **compose 422 @ 271s** | wall clock; residual state also fails `required_control_coverage` |

8 runs executed. 6 clean (`completed` ×5 + one designed `completed_with_failures`),
2 genuine regressions, 1 compose-succeeded-but-empty, 3 wall-clock timeouts.

### The wall clock, with a control

Three composes returned a **server-side HTTP 422 at 270–271s** — g08-s3, g09,
g11 — and the cost table independently corroborates them: `by_status` reports
exactly **3 `timeout` calls** with `model_returned: null` and no token
accounting. These are not client timeouts and not defects; they are the
composer hitting its own wall.

The rate is **3 of 12 composes (25%)**, against round 3's "roughly 1 in 5" — and
round 4 ran **serially** where round 3 ran six concurrently. Serial execution
did **not** reduce the timeout rate, which supports round 3's own nuance that
the wall is not purely a load effect. Running at the package's 240s default
would have made this materially worse; see the ledger note on the two timeout
values.

### Confirm targets

| Ticket | Verdict | Evidence |
|---|---|---|
| `elspeth-47fa7c01eb` | **PASS** | All five surfaces non-500 on a genuine zero-succeeded-token run (g01). Readable — though see the g01 defect: readable is not informative |
| `elspeth-902fc354b2` | **PASS on 2 of 3 samples** | g08-s1 and g08-s2 identical and clean — 12 emitted, 12 terminal, 8 succeeded, **0 failed**, 4 structural, 0 discarded. No `extra_forbidden`. The third sample was lost to the compose wall, so this is 2 valid samples, not the ×3 the brief asked for |
| `elspeth-cd0f6a6cd9` | **PASS** | Authored node carries `profile: acceptance-docs` and relative `key_field: doc_key`, with **no** bucket, region or prefix. The miss failed with `cause: s3_object_unreadable` / `InvalidS3ObjectException`, and `bucket_region_verification` **succeeded** (`observed_region == configured_region == ap-southeast-2`, HTTP 200, `proof_source: response_field`). The discriminating property — not `bucket_region_unverified` — holds. Note the actual code is `s3_object_unreadable`, not the `submit_failed` the brief predicted |
| `elspeth-a79f1b2e6b` | **PASS, comfortably** | USD **0.3215**/session across a full battery — below the ~0.42 target. See Part 4 |
| `elspeth-9d13900064` | not advanced | Its own shape did not run end to end this round |
| `elspeth-cfcd333f83` (g05) | **UNSAMPLED** *(corrected 2026-08-06; originally recorded "RE-CONFIRMS LIVE")* | The g05 compose could not exercise this ticket — see below |
| `elspeth-39118dd24f` (g11) | **inconclusive** | Confounded by the compose wall; the residual state fails `required_control_coverage`, but the compose never completed |
| `elspeth-82d4c5146c` | not sampled | No graph reached its shape |
| `elspeth-49b467d91a` | **UNSAMPLED** | Frontend DOM; needs Playwright |
| `elspeth-454892147c` | **UNSAMPLED** | Needs an induced provider failure; no lever exists |

### g05: compose returns 200 with no state — an honest decline, not a defect *(section corrected 2026-08-06)*

**g05** (`session e3f2036d-…`). Round 3: failed. Round 4:

```
compose HTTP 200 in 53s
reviews clear after 0 pass(es)
validate: 200 is_valid=False
  FAILED: state_exists | No composition state exists for this session
```

**The original reading of this shape ("transport says success and there is
nothing behind it") was wrong**, and the "re-confirms `elspeth-cfcd333f83`"
claim is retracted on the live session transcript plus code evidence:

- The archived 200 body (`/tmp/elspeth-battery/g05/compose.json`) is an honest
  conversational decline with `state: null`: the composer attempted
  `set_pipeline`, the tool rejected it (`.title()` is a forbidden call in the
  `value_transform` expression sandbox), the rejection was disclosed in-loop,
  and the model named the gap and asked the user how to proceed. A compose
  turn that authors nothing correctly returns 200 with a null state — the
  client stack is built on that shape. Resolved as not-a-bug
  (`elspeth-9cd47dc933`); the causal defect — `value_transform`
  `composer_hints` advertising capabilities the sandbox cannot perform — is
  `elspeth-18bcf7dd09`.
- **`elspeth-cfcd333f83` was UNSAMPLED this round**, not re-confirmed: no
  `llm` transform was authored in g05's session, so the output-field collision
  could not occur — and a transform whose config fails construction never
  reaches the build-time collision check at all.

The three 422 timeouts remain honest failure reports; `g09` reaches
`state_exists` after its honest 422. The battery driver's `state_exists`
verdict on g05 was the driver equating HTTP 200 with "pipeline authored" —
on a 200 it must read `body["state"]` and report "composer declined" when
null, instead of a false `state_exists` failure.

### The advisor FLAG rate could not be measured

The brief asks for the Sydney advisor FLAG rate against Singapore's 14-of-16
`verdict=flagged` at `phase=early`. It is **not recoverable from the session
store**: no row in `chat_messages` — in any role — contains the string
`verdict`, and an `audit`-role key census returns empty. The advisor definitely
ran (12 `zai.glm-5` calls across 11 sessions, USD 0.2095), so the verdict is
simply not persisted there. Whatever produced Singapore's 14-of-16 was a
different surface. Reported unmeasured rather than estimated.

### Confirm target `elspeth-47fa7c01eb` — **PASSES**

A run with zero succeeded tokens must be readable. Round 4 produced one
naturally (g01, below) rather than by construction, which is stronger evidence
than a synthetic case. All five named surfaces return non-500:

| Endpoint | Status |
|---|---|
| `/api/runs/{id}` | 200 |
| `/api/runs/{id}/diagnostics` | 200 |
| `/api/runs/{id}/outputs` | 200 |
| `/api/runs/{id}/results` | 200 |
| `/api/sessions/{id}/runs` | 200 |

Readable, but see the next finding: readable is not the same as informative.

### New defect — an accepted invented source has every row discarded at source validation, silently

**g01** (`session e41b2ec2-…`, `run ad27eb80-a69d-45a3-99f4-c76e6b595e53`).
Round 3: `completed`. Round 4: **`empty`**.

The intent asks the composer to invent four sample tickets. It did, correctly:
an `invented_source` interpretation was staged, the driver resolved it
`accepted_as_drafted` (HTTP 200), and the state shows it `resolved` with the
CSV materialised to a blob:

```
source: plugin csv, on_validation_failure: discard
  schema: 4 str fields, guaranteed_fields all 4, mode flexible
  blob_ref: a6e1693d-f72e-4817-aae9-58167a26e81a
  source_authoring: modality llm_generated, resolved_kind invented_source,
                    content_hash a1f08cbf…
```

The run then reports:

```
status "empty"          accounting.source.rows_processed 0
tokens.emitted 0        routing.discarded 0
integrity.closure "closed"   missing_terminal_outcomes 0
discard_summary: total 4, validation_errors 4,
                 stage source_validation, node source_source_109e31cdd1b1
```

So the blob **was** read and four rows **were** parsed — then all four failed
source validation and were discarded.

The data is not the problem, and this was proved rather than assumed. The
recorded `content_hash` is `a1f08cbffe8d06b47a92b1dc9a1cf757202674d2363bbe75f3f9e51f6a0ca319`;
a local file reconstructed from the accepted draft is **524 bytes and hashes
identically**, matching the state's own `accepted_len: 524`. A UTF-8 BOM — the
obvious mechanism for "every row fails on a guaranteed field" — would change
the hash to `6e3cdb85…`, so that hypothesis is **refuted**. The source content
and the declared schema are both correct; the rejection is in the
source-validation path.

#### Follow-up: intermittent, and the discriminator is header case

The intent was re-run twice more on the same live stack:

| Attempt | Session | Run | Outcome |
|---|---|---|---|
| 1 | `e41b2ec2` | `ad27eb80` | `empty` — 4/4 discarded |
| 2 | `6055267c` | `da5cc692` | `empty` — 4/4 discarded |
| 3 | `36082993` | `217e40d8` | **`completed`** — 4 processed, 4 succeeded, 0 failed |

So it is **composition-dependent, not a hard failure** — which also means
"regression against round 3" stays unproven, since a stochastic composer choice
could have gone the other way then. Comparing the source schema across the
three shows what differed:

```
FAILING (×2)   mode: flexible
               fields: [{name: TicketID, field_type: str}, {name: CustomerName, …},
                        {name: Priority, …}, {name: Summary, …}]
               guaranteed_fields: [TicketID, CustomerName, Priority, Summary]

PASSING (×1)   mode: flexible
               fields: ["id: str", "name: str", "priority_level: str", "issue_summary: str"]
               guaranteed_fields: [id, name, priority_level, issue_summary]
```

**The failing schema is the correct one, and the discriminator is not the spec
form — it is case.** CSV headers are normalized to lowercase Python identifiers
at the source boundary (`plugins/sources/field_normalization.py:99`), but
declared `schema.fields` names are used verbatim, so a schema declaring
`TicketID` can never match a row keyed `ticketid`. Every row fails
`model_validate` at `plugins/sources/csv_source.py:558` with four
`Field required [missing]` errors. The bare-string form fails identically
against the same mixed-case header (verified); attempt 3 passed because the
composer also invented a CSV whose headers were already lowercase, not because
the string form bypasses validation. The proposed
`core/dag/schema_validation.py:178-183` mechanism is refuted: the generated
model has four `model_fields` in every case, and that code is build-time edge
validation, not per-row source validation. This does NOT share a root with the
`type_coerce` defect below. The defect dates to RC2 (2026-02-02) and is not a
round-3 regression; it reproduces identically on the YAML surface, so it is not
composer-specific. Filed as elspeth-3664e213c4.

Two reporting defects compound it:

1. **`routing.discarded: 0` contradicts `discard_summary.total: 4`** in the
   same payload. Two views of one run disagree about whether anything was
   discarded.
2. **No surface states why.** `/diagnostics` returns `tokens: []` with empty
   `summary.state_counts` *(field names corrected 2026-08-06:
   `RunDiagnosticsResponse` has no `errors`/`nodes` fields on any version — a
   source-discarded row never becomes a token, so the token-anchored
   projection is empty by construction)*; `/outputs` returns `artifacts: []`;
   the web log group carries no application-level rejection record even with
   `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1` — which is compose-time only and
   was never in scope for runtime row validation. An operator sees a pipeline
   that "did nothing" with no route to the reason.
   `on_validation_failure: discard` is the composer's own default here, so
   this is the default path.

### New defect — a `type_coerce` edge is rejected with no repair suggestion *(mechanism corrected 2026-08-06; original heading claimed the validator was wrong)*

**g02** (`session 481cca93-…`). Round 3: `completed_with_failures` 4/1/1, by
design. Round 4: **fails validation, never runs.**

```
validate: 200 is_valid=False
  graph_structure | Edge from 'source_source_80d272629b7f' to
  'transform_coerce_score_87e360afb307' invalid: producer schema 'CSVRowSchema'
  incompatible with consumer schema 'TypeCoerceInput':
  Type mismatches: score (expected float, got str)
```

**Deterministic, 3 of 3.** Re-run twice more, identical failure with fresh node
ids each time, so the composer re-authors the shape rather than replaying a
cached graph. Contrast g01, which is intermittent.

**The mechanism recorded here on first writing was wrong, and this section
retracts it.** The original reading — "a CSV source yields `str`, `type_coerce`
converts it, so the validator rejects the edge for the mismatch the transform
exists to resolve; `check_compatibility` has no notion of a converting
consumer; localised to `schema_validation.py:178-205`; look in the 35-commit
window" — does not survive the fix work (`e64fa38f2`, `elspeth-aed3b69cf0`):

- **`type_coerce` already models a converting consumer.** Its input schema is
  the declared block verbatim and its *output* overwrites those types with the
  conversion targets. A node's `schema:` block declares what **arrives**, never
  the transformed result.
- **The composer authored a contradictory declaration** — `score: float` on the
  input side of the node that produces the float. That input contract is
  genuinely unsatisfiable, and the build-time rejection is **correct and
  load-bearing**: the strict runtime gate would otherwise have failed 100% of
  rows.
- **Not a regression, and the bisect is retired.** The shape is reachable from
  plain YAML at HEAD, so the 35-commit window is not implicated.
- My composer-independent probe was misread. `examples/transform_pipeline`
  passes because it declares `price: str` — what arrives. It was evidence of
  *correct* behaviour on both sides, not of a bypass that spares untyped
  schemas.

**The real defect is disclosure, not validation.** `EdgeContractError` raised
during graph *build* was caught by phase-1 `validate_graph_structure` as its
superclass `GraphValidationError` with `suggestion=None`. Phase 3's rich
handler needs a built graph and is dead for build-raised errors. So the
composer was told the edge was invalid and never received the
`patch_node_options` repair that would have let it self-correct — which is why
it failed identically on all three samples instead of converging.

Consequence for verification: the fix makes the failure **repairable**, not
absent. A round-5 g02 sample can still fail on the first authoring attempt; the
discriminating property is that the suggestion is now present and the composer
can act on it.

## Part 4 — cost

Measured read-only through `a-9bf256b5c6305b89f30e-database-bootstrap` with the
transaction set `READ ONLY`. Same query shape as the pre-teardown baseline, so
the two are directly comparable.

| Cohort | Sessions | Calls | Tokens | Total USD | **Mean USD/session** | Median | Min | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| R3 pre-cache-fix (08-04) | 18 | 172 | 11,085,834 | 23.25 | 1.2915 | 1.1159 | 0.4776 | 3.0940 |
| R3 post-cache-fix (08-05) | 12 | 98 | 5,675,313 | 5.93 | 0.4946 | 0.4806 | 0.1901 | 0.7442 |
| **Round 4 (Sydney)** | **13** | **87** | **4,296,867** | **4.18** | **0.3215** | **0.2806** | **0.1540** | **0.5700** |

**USD 0.3215 per session** — a 75% reduction against the round-3 baseline and
35% below the post-fix cohort, under `elspeth-a79f1b2e6b`'s ~0.42 target.

**This is a floor, not a full-battery figure.** Three composes died at the wall
and one graph failed validation before executing, so only 8 of 13 sessions
produced runs: round 4 bought part of its saving by doing less work. The
ticket's criterion is explicitly a *full* battery, so this does not satisfy it
and the ticket should not close on it — treating 0.3215 as the answer would
repeat exactly the small-sample error this report identifies in the brief's own
0.4246 figure. The per-call figures below are the load-independent comparison
and are the sounder basis for judging the cache fix.

### Composer and advisor lines, separately

| Model | Role | Calls | Tokens | USD | Sessions | USD/call |
|---|---|---:|---:|---:|---:|---:|
| `au.anthropic.claude-sonnet-4-6` | composer | 75 | 4,095,466 | 3.9701 | 13 | 0.0529 |
| `zai.glm-5` | advisor | 12 | 201,401 | 0.2095 | 11 | **0.0175** |
| _R3_ `claude-haiku-4-5` | _advisor_ | _28_ | _618,154_ | _0.7222_ | _28_ | _0.0258_ |

**GLM-5 is cheaper per advisor call than Haiku was** — USD 0.0175 against
0.0258, about 32% less — while being a different vendor, which is the point of
the swap. The advisor is USD ~0.019 per session, 5.0% of the round-4 total.
The model change cannot move the headline cost either way; it must be judged on
advisory quality, which this round could not measure (see above).

No silent substitution: `model_requested` and `model_returned` agree on all 84
successful calls (`au.anthropic.claude-sonnet-4-6` ×72, `zai.glm-5` ×12). The 3
disagreements are the timeouts, which return `null`.

### Tokens by call index

| Call index | Samples | Avg total | Min | Max |
|---:|---:|---:|---:|---:|
| 1 | 13 | 51,110 | 16,704 | 70,149 |
| 2 | 13 | 47,716 | 16,661 | 66,139 |
| 3 | 13 | 49,988 | 16,604 | 62,979 |
| 4 | 13 | 50,783 | 16,368 | 64,425 |
| 5 | 11 | 48,869 | 17,075 | 71,124 |
| 6 | 7 | 54,257 | 16,723 | 66,087 |
| 7 | 6 | 49,994 | 16,410 | 63,771 |

Flat across the first seven calls at ~48–54k, against round 3's 58–77k on the
same measure. The ~16–17k minima are the single-call GLM-5 advisor
consultations. Cost continues to scale with **call count**, not with per-call
context growth — the round-3 finding holds.

`reasoning_tokens` totalled 19,386 across all successful calls.

## Recommended closes — operator sign-off only, nothing closed here

Close carries `close_commit release/0.7.2@<tip>`. Stochastic items never close
on a single pass.

| Ticket | Recommendation |
|---|---|
| `elspeth-cd0f6a6cd9` | **Recommend close.** ADR-036 shape confirmed on a fresh Region with a fresh grant; region verification demonstrably succeeded and the miss failed on object readability |
| `elspeth-47fa7c01eb` | **Recommend close** on the stated criterion (five surfaces non-500). Consider a sibling for "readable but carries no reason" — see the g01 defect |
| `elspeth-a79f1b2e6b` | **Do not close — re-measure.** 0.3215/session is under target, but the ticket's criterion says a **full** battery and this one was not: 3 composes died at the wall and 1 graph failed validation, so only 8 of 13 sessions produced runs. 0.3215 is a **floor from a partial battery**. Closing on it would repeat the small-sample error this report criticises in the brief's own 0.4246 figure |
| `elspeth-902fc354b2` | **Do not close.** Measured 1-in-4 intermittent; 2 clean samples is not the ×3 the brief required. Re-sample |
| `elspeth-cfcd333f83` | **Keep open, update.** Re-confirmed live with a new, sharper symptom: compose returns 200 in 53s and creates no composition state |
| `elspeth-39118dd24f` | **Keep open, unverified.** Confounded by the compose wall this round |
| `elspeth-9f7d336e1c` | **Negative datum recorded.** A freshly published image from `52ab3ec8b` cold-installed, booted, and passed 35/35 doctor checks in a new Region. The defect did not manifest |
| `elspeth-671a17d5c0` | **Keep open.** Three new package findings below |

## New defects to file — label `battery-2026-08-06`

| # | Severity | Summary |
|---|---|---|
| 1 | P1 | An accepted `invented_source` has 100% of its rows discarded at source validation. Content and schema both proven correct (hash-matched); `on_validation_failure: discard` is the composer's own default, so the rows vanish silently. **g01, round-3 regression** |
| 2 | P1 | The edge validator rejects `type_coerce` for the exact type mismatch it exists to resolve, making it unbuildable downstream of any CSV source. **g02, round-3 regression** |
| 3 | P2 | Compose returns **HTTP 200** having created no composition state (g05, 53s — not a timeout). Transport success with nothing behind it |
| 4 | P2 | `routing.discarded: 0` contradicts `discard_summary.total: 4` in the same response payload |
| 5 | P2 | No surface discloses *why* rows were discarded — `/diagnostics` returns `errors: []` and `nodes: []`, and nothing reaches CloudWatch even with `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1` |
| 6 | P2 | The runbook's zero-finding ECR scan gate is unsatisfiable for the cloudwatch-agent image; all 33 findings are unfixed-upstream OS packages from a `python:3.13-slim` runtime the image never uses |
| 7 | P2 | Terraform teardown does not deregister task definitions registered out of band. 12 survived this run's destroy; 39 survived an earlier one |
| 8 | P3 | The advisor verdict is not persisted to the session store, so the FLAG rate cannot be measured from it |
| 9 | P3 | Four battery-required settings are not Terraform inputs, and the package's default plugin allowlist (15) is not the share round 3 tested (40) |

Findings 1, 2 and 3 are the priority. *(Corrected 2026-08-06: the original text
called all three regressions against round 3 and sent the reader to the
35-commit window between the two images. **None of the three is a regression,
and the bisect is retired.** Finding 1 is header-case normalization, dating to
RC2 and reachable from plain YAML; finding 2 is a missing repair suggestion on
a correct build-time rejection; finding 3 was resolved not-a-bug — a compose
that authors nothing correctly returns 200 with a null state, and its causal
defect is `elspeth-18bcf7dd09`. Round 3's green on g01 and g02 was composition
luck, not a working code path.)*

## AWS ledger

| # | When (UTC) | Mutation | Region | Result |
|---|---|---|---|---|
| 1 | 2026-08-05T18:5x | read-only session-store aggregates ×3 via `a-fa1b99c60192978b10f7-database-bootstrap:6` | ap-southeast-1 | exit 0; no writes (session `READ ONLY`) |
| 2 | 2026-08-05T19:0x | `terraform apply` scenario-a **destroy** | ap-southeast-1 | 86 destroyed; `state list` empty |
| 3 | 2026-08-05T19:1x | emptied backend bucket `elspeth-tfstate-cf548430ae8b` | ap-southeast-1 | 28 versions + delete markers removed after census |
| 4 | 2026-08-05T19:1x | `terraform apply` bootstrap **destroy** | ap-southeast-1 | 10 destroyed; `state list` empty |
| 5 | 2026-08-05T19:1x | deregistered 12 out-of-band ACTIVE task definitions | ap-southeast-1 | `…-web:7–15`, `…-runtime-doctor:7`, `…-schema-init-doctor:7–8` |
| 6 | 2026-08-05T19:2x | Container Insights log-group cleanup | ap-southeast-1 | stable: 1 deletion, 608s quiet, 57 samples |
| 7 | 2026-08-05T18:5x | `terraform apply` bootstrap **create** | ap-southeast-2 | 10 created; bucket `elspeth-tfstate-700e19d57894`, 2 ECR repos, boundary |
| 8 | 2026-08-05T19:0x | ECR push `elspeth-web:acceptance-700e19d5-…` | ap-southeast-2 | index `sha256:fabe0cf9…`, amd64 child `sha256:d614f114…` |
| 9 | 2026-08-05T19:0x | ECR push `elspeth-cloudwatch-agent:agent-52ab3ec8b646` | ap-southeast-2 | index `sha256:5e95635d…`, amd64 child `sha256:4e8cca34…` |
| 10 | 2026-08-05T19:1x | ECR Basic scans | ap-southeast-2 | web child **COMPLETE, 0 findings**; agent child **COMPLETE, 33 findings** — gate failed, proceeded deliberately |
| 11 | 2026-08-05T19:2x | `terraform apply` scenario-a **create** | ap-southeast-2 | **86 created**; 91 in state incl. 5 data sources |
| 12 | 2026-08-05T19:2x | EFS provision one-shot `…-payload:1` | ap-southeast-2 | exit 0 |
| 13 | 2026-08-05T19:2x | schema-owner doctor `…-schema-init-doctor:1` (`--init-schema`) | ap-southeast-2 | exit 0 |
| 14 | 2026-08-05T19:2x | runtime-credential doctor `…-runtime-doctor:1` | ap-southeast-2 | exit 0 |
| 15 | 2026-08-05T19:3x | `update-service` → `…-web:1`, desired 1 | ap-southeast-2 | single PRIMARY COMPLETED, `failedTasks: 0`; `/api/health` + `/api/ready` 200 |
| 16 | 2026-08-05T19:3x | registered `…-web:2` (4 settings, normalised diff clean) | ap-southeast-2 | **stack installed at `COMPOSER_TIMEOUT_SECONDS=240`; battery ran at 270** |
| 17 | 2026-08-05T19:3x | `update-service` → `…-web:2` | ap-southeast-2 | single PRIMARY COMPLETED; endpoints 200 |
| 18 | 2026-08-05T19:3x | doctor on `…-web:2` as command override | ap-southeast-2 | exit 0, **35/35 ok**, session + landscape schema `current` |
| 19 | 2026-08-05T19:3x | S3 put `…/docs/exec-summary.pdf` (176,058 B) | ap-southeast-2 | present; quarantine key confirmed **absent** |
| 20 | 2026-08-05T19:4x | battery: 13 sessions, 8 runs | ap-southeast-2 | see Part 3 |
| 21 | 2026-08-05T19:5x | read-only cost + advisor aggregates ×2 | ap-southeast-2 | exit 0; no writes |

Both composer-timeout values are recorded deliberately. The stack was
**installed and doctor-qualified at the package's own 240s**, which is what
`elspeth-671a17d5c0` needs to know. The **battery ran at 270s**, matching round
3, so the per-graph table is comparable; at 240 every timeout would have been
unattributable between "regressed" and "had 30 seconds less".

## Not done

- `elspeth-49b467d91a` and `elspeth-454892147c` remain unsampled, as the brief
  anticipated. Reported, not faked.
- The g01 root cause is characterised but not isolated to a commit. The data
  and schema are proven correct; the fault is inside source validation.
- The Sydney stack is **left running** for follow-up investigation. Its
  cleanup deadline is 2026-08-09.
