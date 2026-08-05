# Brief: unblock the compose cost measurement by fixing the acceptance allowlist

Date: 2026-08-05. Author: `claude-cache-deploy`. Audience: the agent that
executes it. Prerequisite reading:
`docs/acceptance/2026-08-05-compose-token-cost-addendum.md` (the fix being
measured) and `ops-local/README.md` (environment of record, operator
utilities). Status at handoff: the cache fix (`6bcd69037`,
elspeth-a79f1b2e6b) is **deployed and live** as
`a-fa1b99c60192978b10f7-web:11`; the post-fix cost number is **unmeasured**
because the measurement compose could not complete.

## Why the measurement is blocked

The deployed `ELSPETH_WEB__PLUGIN_ALLOWLIST` (15 plugins, 7 transforms)
omits every deterministic value transform. Battery graph g01 ("rename
columns, lowercase a priority word, shorten a summary to ~80 chars, write
CSV") therefore forces the composer to author an `llm` transform for two
string operations, which in turn triggers the required-control splice
(`content_safety`/`prompt_shield` are `required`, and coverage fires only
when an LLM node exists — `required_controls.py:703-709`,
`coverage.py:376-378`). The resulting graph needs ~4 authoring turns at
30–130s per turn and dies at the 270s
`ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` wall clock
(`convergence_wall_clock_timeout`, HTTP 422, observed live 2026-08-05
session `4f5b8a1f-407e-44f1-9a54-d3c6a6470ce8`).

This was verified as a forcing, not a composer preference: two blind
subagent authoring runs given the same intent and allowlist both reached
for `llm` and both stated no deterministic alternative existed. Do not
re-litigate that finding; execute the unblock.

## Task 1 — register and deploy `web:12` with three transforms added

Change **only** the allowlist env value on the web container. No image
change, no ECR work, no schema work (SESSION_SCHEMA_EPOCH stays 45).

1. Describe the live TD (`a-fa1b99c60192978b10f7-web:11`,
   `--include TAGS`). Work from the live JSON, never a cached copy.
2. Edit `ELSPETH_WEB__PLUGIN_ALLOWLIST` on the `elspeth-web` container
   (leave `cloudwatch-agent` untouched): append
   `"transform:value_transform"`, `"transform:truncate"`,
   `"transform:type_coerce"` to the existing 15 entries. Keep JSON-array
   formatting identical in style (compact, double-quoted).
3. Bump `ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION` to the
   new revision number. Leave `..._RELEASE` alone — the image and git SHA
   are unchanged.
4. **Replicate the TD tags into the registration.**
   `RegisterTaskDefinition` is condition-gated on
   `aws:RequestTag/ACCEPTANCE_RUN_ID` and is AccessDenied without them.
   Tags sit beside `taskDefinition` in the describe response but inside
   the register input.
5. Prove the narrow diff before registering: a normalised comparison must
   show exactly two changed values (`PLUGIN_ALLOWLIST`,
   `TELEMETRY_TASK_DEFINITION_REVISION`) and nothing else — no sidecar,
   secret, role, mount, logging, or other env drift. Abort on any third
   difference.
6. Do NOT edit `ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256` by hand.
   If any gate reports a policy-binding mismatch after deploy, stop and
   surface it to the operator — that hash is an integrity seal, not a
   config knob.
7. Run the pre-deploy doctor **on the candidate web TD itself**
   (`ops-local/acceptance/run_task.py a-fa1b99c60192978b10f7-web:12
   --overrides file://doctor-override.json`, override command
   `["doctor","aws-ecs","--json"]`). The doctor family lacks the
   profile-grant env; only the web TD validates it. Expect all checks ok
   and `session_schema: current`. Read the `elspeth-web` container's exit
   code, not `containers[0]` (the sidecar leads).
8. `update-service` to `web:12` with
   `minimumHealthyPercent=0,maximumPercent=100,desiredCount=1`. Wait for
   1/1/0 with a single PRIMARY/COMPLETED deployment. Verify
   `/api/health` and `/api/ready` both 200 at
   `https://elspeth.aws.foundryside.dev`.
9. Confirm the change landed: `GET /api/composer` discovery (or a fresh
   session's `list_transforms`) must now offer `value_transform`,
   `truncate`, `type_coerce` and must still list the guardrail plugins.

Rollback candidate is `web:11`. Rollback is safe (env-only change, same
image, same schemas).

## Task 2 — serial composes, then the cache-split cost query

**Never parallelise composes** — one 1 vCPU task; parallel composes
manufacture timeout artifacts. Strictly one at a time.

1. Drive g01: `python3 ops-local/acceptance/drive_graph.py g01 --no-run
   --compose-timeout 280` (from `ops-local/acceptance/`; note the
   filigree-CLI-in-worktrees caveat does not apply, but the persistent
   shell CWD does — prefer absolute paths). A client timeout does not
   abort the server-side compose; reconcile by re-reading the message
   list before declaring failure.
2. Then g02, then (only if both completed) g04. Three completed composes
   give a defensible mean; two is the minimum for reporting.
3. Sanity-check each authored graph before crediting it:
   - **The fake-sample-data trap:** the intent asks the composer to
     invent rows. A graph can look lean because it authored sample data
     already lowercase/short and skipped the transforms. Inspect the
     inline blob: g01 sample tickets must include mixed-case priorities
     and >80-char summaries, and the graph must actually wire the
     transform nodes.
   - Expected post-fix shape for g01: deterministic transforms
     (`field_mapper` + `value_transform`/`truncate`), **no `llm` node,
     no guardrail nodes** (coverage is conditional on an LLM node).
     If an `llm` node still appears, record why (read the assistant
     turns) — that is a composer-choice finding, not a policy forcing,
     and changes the interpretation of every number.
4. Run the read-only cache-split query:
   `python3 ops-local/acceptance/run_task.py
   a-fa1b99c60192978b10f7-database-bootstrap --overrides
   file://cache-cost-override.json` (regenerate first via
   `python3 ops-local/acceptance/make_cache_cost_override.py` if the
   JSON is missing). The query already separates cohorts: rows lacking
   the `cache_read_input_tokens` key are the pre-fix build (17 sessions,
   168 calls, **USD 22.77 total, USD 1.3394/compose** — the baseline).
   Post-fix rows carry the full C6 field set.
5. Report, per post-fix session and in aggregate:
   - USD per compose vs the 1.3394 baseline;
   - `cache_read_input_tokens` on warm calls (should approximate
     prefix+history size);
   - `(total_tokens − cache_read_input_tokens)` by call index — the
     addendum's prediction is this **flattens to the per-turn delta**
     instead of growing monotonically;
   - `cache_creation_input_tokens` concentrated in each session's first
     call.
   Wall-clock is a non-metric for cost; report it only for the Task 3
   decision.
6. Caveat to carry into the report: if the post-fix graphs are
   deterministic (no LLM node), the per-compose dollar figure reflects
   BOTH the cache fix AND the cheaper graph shape. Separate the effects:
   the cache split (point 5) isolates the caching behaviour regardless
   of graph shape; say explicitly which lever produced which saving. If
   a clean same-shape comparison is wanted, drive one additional graph
   whose intent legitimately needs an LLM node (g07 or g08 family) and
   compare its call-index curve against the pre-fix cohort's.

## Task 3 — the timeout decision (decide, don't tune)

Rule: if all Task-2 composes complete within the existing 270s budget,
**leave `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` alone** and record that no
change was needed. Only if a compose still times out post-allowlist:
diagnose per-turn latency first (CloudWatch log group
`/aws/ecs/a-fa1b99c60192978b10f7-web`, stream
`web/elspeth-web/<task-id>`; `elspeth-installer` can `GetLogEvents` but
not `FilterLogEvents`). Known contributor: the freeform loop's reasoning
hint resolves to `thinking: {type: adaptive}` on Bedrock (model picks its
own budget — `web/composer/reasoning.py`, `config.py:207`); that is
ticket material (see the action list), not a reason to raise the budget.
Raising the budget to absorb unbounded thinking is the failure mode this
rule exists to prevent. Any budget change is an operator decision —
surface it, do not self-serve it.

## Traps inherited from this environment (all bitten before)

- One-shot TDs (`schema-init-doctor`, `runtime-doctor`) pin stale images
  — irrelevant here (no image change), but do not run them casually.
- The ECR scan gate reads the platform child manifest, not the OCI index
  — irrelevant here (no push), noted in case of scope creep.
- `analyse_compose.py` reports phantom "IDENTICAL repeat calls" — it
  truncates the call signature at 400 chars. Read the assistant prose
  before believing a repeat.
- The store keeps pre-fix rows; never average across cohorts. The
  `cache_read_input_tokens` key-presence test is the discriminator.
- Output-file mtime is not a liveness signal; the audit trail is.
- `cd`-prefixed commands flip the persistent shell CWD; use absolute
  paths or `git -C`.

## Success criteria

1. `web:12` live, stable 1/1/0, health/ready 200, allowlist visibly
   extended, no other TD drift.
2. ≥2 composes completed serially with genuinely-exercised transforms
   (sample-data trap checked).
3. A post-fix USD/compose figure and cache split reported against the
   USD 1.3394 / 22.77 baseline, with the flattening curve shown by call
   index, and the graph-shape caveat stated.
4. A one-line Task-3 verdict: budget untouched (expected) or a diagnosed
   escalation.
5. Findings appended to the acceptance docs and
   elspeth-a79f1b2e6b updated with the measurement evidence.
