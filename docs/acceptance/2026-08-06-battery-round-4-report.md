# Battery round 4 — tear down Singapore, cold-install Sydney

Status: **in progress**. Started 2026-08-06. Brief:
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

_pending_

## Part 3 — battery round 4

_pending_

## Part 4 — cost

_pending_

## AWS ledger

| # | When (UTC) | Mutation | Region | Result |
|---|---|---|---|---|
| — | 2026-08-06 | read-only session-store aggregates ×3 via `a-fa1b99c60192978b10f7-database-bootstrap:6` | ap-southeast-1 | exit 0; no writes (session set `READ ONLY`) |
