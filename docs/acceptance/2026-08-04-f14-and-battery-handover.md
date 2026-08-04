# Handover: resolve F14 and run acceptance battery round 2

Date: 2026-08-04 (evening). Author: claude-grounded-custodian (Claude Fable
session, campaign custodian). Audience: the incoming session that will (A)
land the F14 fix cluster and (B) run the 10-graph acceptance battery. The
campaign tracker (`docs/acceptance/2026-08-03-r3-rca-remediation-tracker.md`)
is the running state of record; this brief is the working instruction for the
next two objectives only.

## Baseline at handover

| Fact | Value |
|---|---|
| Release tip | `d5604e0c0` on `release/0.7.2`, clean, **pushed** to origin |
| Last full suite | **37,407 passed / 27 skipped / 1 expected trust-tier xfail, exit 0** at `9ab853d01` (wave-2 reconciliation; nothing code-bearing landed since — later commits are docs-only) |
| Staging | `elspeth-web.service` serves the main checkout, restarted 16:03 after wave 2, healthy (`/api/health`, `/api/ready` 8/8) |
| AWS acceptance | https://elspeth.aws.foundryside.dev (durable Amazon cert to 2027-02-12; the bare apex deliberately does not resolve). Deployed release `67e2a1661` — **pre-wave-2**; task-def `a-fa1b99c60192978b10f7-web:8`, cluster `acceptance-a-fa1b99c60192978b10f7-cluster` |
| Tutorial canary | GREEN at `449c93397` (staging, Landscape-verified) |
| RDS budget of record | approved-budget=50 / safety-margin=50 (operator-ratified; high-water 6 vs max_connections 194) |

Working discipline (unchanged from AGENTS.md plus session precedent): Filigree
writes via CLI `--actor <lane-actor>` from the main checkout (MCP writes
conflict on actor identity); claims atomic (`start-work`/`reclaim`), never
claim+status chained; on `SCHEMA_MISMATCH` stop. Worktrees under
`.claude/worktrees/<name>` with `.venv` symlinked to the main checkout — never
bare `uv pip install`; test with `PYTHONPATH=<worktree>/src`. Commits: pathspec
staging, and in the main checkout use path-limited commits
(`git commit -m … -- <paths>`) — other sessions share the index. Never
`--no-verify`. Push: `gh auth switch --user johnm-dta`, push, switch back to
`tachyon-beep`. AWS: environment is fully disposable (operator grant:
mutate/add/remove at will) but Route53/NS/ACM survive every teardown; ONE
credential refresher (if expired, ask the operator); deploys under
`elspeth-installer`, IAM policy work under `elspeth-acceptance` (root). The
local permission classifier blocks `kill` into containers regardless of AWS
grants — and the cloudwatch-agent sidecar is distroless (no `kill`, no shell),
so in-container signalling is impossible anyway.

## Part A — resolve F14 (elspeth-5904b1683a) and its companion

### Root cause (settled — do not relitigate; full RCA in ticket comment 2285)

Commit `8ba6f97a8` (2026-08-03) added
`withhold_candidate_facts = canonical_json(finalized_pipeline) != canonical_json(pipeline)`
at `src/elspeth/web/composer/pipeline_planner.py:2854`. When true,
`_allowlisted_candidate_feedback` strips the `plugin_options_invalid`
validator `detail` and masks `component` to `"pipeline"`. On guided surfaces
the predicate is **always true** — `bind_guided_reviewed_components`
(`guided/planning.py:796`) wholesale-replaces source/output options with
reviewed-authority values, and `wire_required_controls` mutates too — so
guided repair feedback is permanently blind: the static `suggested_fix` says
"apply exactly what 'detail' names" (detail absent) and `repeat_notice` says
"change ONLY the fields the errors below name" (no fields named). The model
resubmits ≈the same candidate → `repeated_fingerprint` → `repair_budget=2`
burned → `REPAIR_EXHAUSTED` → HTTP 502 "The provider returned an invalid
response" (`sessions/routes/guided_operations.py:87`). Live measurement: 4/5
canonical replans failed; plain-guided session 3/3. Exhaustion is
deterministic given any invalid first candidate; only the first candidate is
model-stochastic. The custody concern behind `8ba6f97a8` is genuine (validator
messages about server-BOUND components can leak reviewed private values); the
defect is scope — candidate-global where it must be entry-scoped. This is the
fifth confirmed instance of the "one predicate across opposite-safety
contexts" house pattern.

### The fix (four parts, all required)

1. **Entry-scoped withholding.** The finalization path reports which
   component ids it owns/mutated (reviewed-authority replacements +
   auto-wired controls); the feedback builder withholds detail ONLY for
   those, keeping true component id + validator detail for model-authored
   nodes. Seam: likely the finalizer protocol in `composer/service.py`
   returning the owned-id set beside the finalized pipeline; a materially
   better seam is acceptable if recorded.
2. **Honest blind-mode text** (`composer/tools/generation.py`): when detail
   is absent, guidance must not reference "what detail names" — direct the
   model to re-derive options from `get_plugin_schema` for the named kind.
3. **Short-circuit repeated-fingerprint-while-blind**: fail fast to the
   terminal path instead of burning remaining budget; budget semantics
   unchanged when detail IS present.
4. **Honest failure envelope**: planner exhaustion must stop presenting as
   `invalid_provider_response`/502 provider-blame. New code (e.g.
   `planner_repair_exhausted`) through `guided_operations.py` AND the
   frontend mapping — the SlotType/`guided.ts` mirror pre-commit hook
   enforces lockstep; check how the frontend switches on status codes before
   changing the HTTP status.

Worktree `/home/john/elspeth/.claude/worktrees/f14-repair-feedback` (branch
`claude/fix-f14-repair-feedback`, base `9ab853d01`) exists with ONE uncommitted
modified file (`pipeline_planner.py`) from an interrupted agent — inspect the
diff; restarting from spec is likely cheaper than archaeology. Ticket is
`fixing`, assignee `claude-f14-fix` (reclaim under your actor).

Tests: the disagreement regression (guided candidate where finalizer mutates
server-owned options AND the model authored an invalid option on its own node
→ that error keeps true component + detail; server-owned errors stay
withheld); fingerprint-while-blind short-circuits; envelope test both sides of
the wire; `8ba6f97a8`-era custody tests must stay green. Gates before freeze:
ruff, mypy, localized pytest (planner + generation + guided_operations route
+ frontend vitest for the mapping), `scripts/wardline_gate.py`. NO full suite
in-lane.

### Companion: elspeth-49b467d91a (plain-guided prompt loss)

P1, demo-visible, compounds F14: the plain guided surface empties the typed
prompt after an operation failure; the tutorial frame preserves it. Fix by
parity with the tutorial frame's retention. Worktree
`.claude/worktrees/guided-prompt-retention` (branch
`claude/fix-guided-prompt-retention`, base `9ab853d01`) has uncommitted edits
to `ChatPanel.tsx` + its test from the interrupted agent — plausibly close to
done; inspect first. Gates: tsc, lint, vitest. Also open on the same surface
family: elspeth-308d1e0831 (P2, freeform session title renders raw LLM output
— fix if cheap while in the component, else leave claimed-out).

### Landing sequence (wave 3)

1. Freeze each candidate (single commit, exact hash reported).
2. Independent graded review per candidate on the frozen hash — findings
   IN-SCOPE (contradicts ticket/RCA/regresses `8ba6f97a8` custody) block;
   OUT-OF-SCOPE gets filed, never blocks. This discipline is why wave 2
   landed first-round; keep it.
3. `--no-ff` merge(s) into `release/0.7.2`, per-lane canaries, then ONE full
   `pytest tests/ -n 12` at the integrated tip (expect ~37.4k; trust-tier
   xfail expected). Any unexpected failure: stop, diagnose
   pre-existing-vs-introduced before touching anything (wave 2 precedent:
   both failures were baseline hygiene).
4. Bookkeeping: tickets → `verifying` with `fix_verification` set; tracker
   rows + dated timeline entry; remove the two lane worktrees + branches
   (`-d` only); restart `elspeth-web.service` (staging serves this checkout);
   push via the gh-auth switch procedure.

## Part B — acceptance battery round 2 (the 10-graph battery)

### Prerequisite: wave-3 redeploy

Redeploy the acceptance environment to the post-wave-3 tip per
`docs/runbooks/aws-ecs-existing-service-redeploy.md` (update-in-place; the
2026-08-04 deploy record in the tracker shows the full working procedure:
ECR push with revision label, TD revision, zero-overlap update, digest-chain
verification). Two task-def env ADDITIONS this time:

- `ELSPETH_WEB__AWS_S3_SOURCE_PROFILES=<profile config>` — without it
  `source:aws_s3` is absent from the compiled catalog and the S3/Textract
  graphs cannot compose. Discover the expected value shape from the setting's
  consumer (`grep AWS_S3_SOURCE_PROFILES` in src + settings docs); the
  operator can supply bucket/prefix specifics if a choice is needed.
- `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1` — leak-safe (component+code only);
  makes any future F14-class failure diagnosable from logs.

Step-2 IAM policies were re-rendered/attached today (control-plane v5,
regional-resources v4) — re-diff against the tree before the run; re-render
only on drift.

### The battery

Ten Composer-authored graphs against `https://elspeth.aws.foundryside.dev`
(external traffic; in-task verifiers use loopback `127.0.0.1:8451` — hairpin
is blocked). Mandatory anchors:

1. **Row-union A/B test**: ONE dataset, TWO LLM prompts over it, row_union
   topology combining both arms (the A/B comparison outcome). Store-epoch
   discipline: SQLITE_SCHEMA_EPOCH 30 / SESSION_SCHEMA_EPOCH 43 — reset any
   stale local stores first (stale DBs manufacture false
   SchemaCompatibilityError).
2. **Textract graph**: S3-sourced document through Textract. Doubles as live
   acceptance for D6 (`elspeth-1033d97b6c`: region must be
   deployment-derived, bucket-region check enforced — verifying) and for the
   quarantine-explanation check (`elspeth-6801b71f71`): include one
   quarantine-able row (e.g. a nonexistent object) and verify the run
   diagnostics + Landscape explain the quarantine without DB access.

Remaining eight: spread across sources (csv, json, text, llm, s3), sinks,
and topologies — at minimum one fork/merge, one gate with named routing +
an `on_error` path exercised by a poisoned row, one linear multi-transform,
one sink-variety graph. Randomise within that coverage.

## Addendum — round-3 redeploy additions (2026-08-04, ADR-036)

The Textract profile-bound bucket fix (`elspeth-cd0f6a6cd9`, ADR-036) changes
the round-3 task definition and store discipline:

- **New env ADDITION** `ELSPETH_WEB__AWS_TEXTRACT_PROFILES` — JSON array of
  operator document grants, e.g.
  `[{"alias": "acceptance-docs", "bucket": "<app bucket>", "key_prefix": "<org prefix>"}]`
  (same bucket + prefix as the existing S3 source grant; the alias *name* may
  be shared because grants are kind-qualified). Without it the Textract
  transform is honestly unauthorable on the web surface
  (`profile_unavailable`) — the old region-only `deployment` alias no longer
  exists.
- **Session store epoch is now 45** (was 44): the projection flip invalidates
  sessions authored against the old public schema. Delete `sessions.db` / RDS
  session stores before the round-3 run; `auth.db` is never touched.
- `verify-textract` now also proves each configured profile: binding validity
  against the engine's bucket-mode rules and a negative-space
  StartDocumentAnalysis probe against the profile's granted location. Its
  exec receipt gains `profiles_configured` + `profile_locations_invocable`.
- The round-3 Textract anchor (#2 above) must be authored profile-first:
  select `profile: acceptance-docs`, rows carry **relative object keys
  only** — `bucket_field` is no longer web-authorable. The custody check is
  falsifiable: after the run, zero persisted call records may contain the
  bucket literal (query the Landscape `calls` request payloads).

Per-graph verification: the **Landscape audit trail** (run status, row/token
counts, node states, sink effects) — never output-file mtimes. Capture run
ids in the report.

### Sampling riders on the battery

- **F14 post-fix**: run the canonical guided transforms replan ≥10 times;
  record N/M completions. Pre-fix live rate was 1/5; the fix should take
  this to ≈10/10 (any residual exhaustion now carries actionable detail in
  the planner logs).
- **Retry-exhaustion** (`elspeth-454892147c`, verifying): if a retry-enabled
  pipeline with induced provider failures is constructible, sample it
  (~1/16 per run at the historical rate); otherwise report unsampled.
- Advisor END-gate FLAG→repair→CLEAN cycle: not observable in round 1;
  attempt to construct one deliberately FLAGged compose and drive it
  through repair to CLEAN.

### Reporting and sign-off

One tracker timeline entry (path-limited commit) with: per-graph verdict
table + run ids, sampling ratios, and any new defects (file in Filigree with
label `battery-2026-08-XX`). Evidence comments on tickets the battery
discharges. **Recommend closes; do not close** — operator sign-off closes
(today's precedent: closes were operator-authorized then executed with
`close_commit release/0.7.2@<tip>`). Stochastic items never close on a
single pass.

## Queue after these two objectives (for orientation, not execution)

Grounded lanes B (`elspeth-826765af90`: StatedOptionValueConstraint +
grounding + projection + SESSION_SCHEMA_EPOCH 43→44 with sessions.db wipe at
landing; reference tree preserved in worktree `grounded-option-constraints`
at `a5d7fc0e7`) and C (`elspeth-e75dc03d3e`: custody hardening incl. the
duplicate-verification fix); retention-completion follow-up
(`elspeth-a96b2f1b0a` residual — now unblocked, lane-A files); wave-2 residue
tickets (`8e44675d36`, `e01f75b034`, `c3727c7732`, `1947b6da30`,
`c6ababad46`); the advisor/compose-loop/gate-routing epics; the AWS
least-privilege install lane (includes the outage-phase runbook defect — the
sidecar cannot be signalled). Two checker-script defects
(`elspeth-5824bd9546`, `elspeth-0a9d1e89a9`) still make
verify-operator-telemetry fail spuriously; delivery is proven, fix the
checkers when touched.
