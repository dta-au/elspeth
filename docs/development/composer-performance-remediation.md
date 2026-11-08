# Composer performance remediation — plan of record

Status: **open**. Baseline measured on `main` at `3b28d537` (0.8.9). Each fix
below is worked in sequence; update its status row and "Evidence" line as it
lands. This is a working plan, not a specification: it is updated or deleted
as the system changes.

## Why this exists

A profiled tutorial composition run (local PostgreSQL, request profilers plus
SQL event logging, Azure prompt guard excluded via local development mode)
identified four avoidable-work hotspots in the composer plane and one
measurement that needs follow-up. Source verification against `3b28d537` is
recorded in the per-fix sections; every mechanism the profile named exists in
the code as described, with two corrections noted (fixes 2 and 3).

### Baseline (one run; not p95)

| Measurement | Initial composition loop | Composition through plugin outcome |
|---|---:|---:|
| Elapsed | 21.70 s | 110.02 s (includes ~30 s harness delay) |
| Backend CPU | 2.31 s | 24.38 s |
| SQL statements | 1,157 | 4,359 |
| SQL cursor time | 1.27 s | 8.88 s |
| Explicit clock queries | 385 | 942 |
| Commits / rollbacks | 117 / 311 | 600 / 944 |
| Backend RSS | 423.9 → 435.5 MiB | peak 518.1 MiB |

Planner calls on the initial proposal: two, ~10 s each; 113,143 logical input
tokens (49,664 cached), 9,522 output tokens (8,213 reasoning).

Process-wide figures including background coordination. The 409 from the
pipeline was correct plugin output and is out of scope.

## Fix sequence and status

| # | Fix | Risk | Complexity | Gate exposure | Status |
|---|---|---|---|---|---|
| 1 | Cache immutable catalog summaries | Low | Low | Unit | open |
| 2A | Route interpretation-resolve validation through the queue | Low | Low | Vitest | open |
| 3-T1 | Stream read consolidation (no semantic change) | Low–Med | Medium | **Testcontainer**, ADR-047 | open |
| 2B | Share validation result with readiness | Medium | Medium | Vitest + readiness unit | open — decision needed |
| 4A | Per-component prompt token accounting | Low | Low | None | open |
| 4B | Reasoning-effort tuning | Medium (quality) | Low | — | open — applies on OpenRouter test models |
| 3-T2 | Snapshot-bound per-send stream authorization | High (semantic) | Low code | Developer ruling | not started — ruling needed |
| 5 | Allocation profiling | — | — | — | after 1 and 2A |
| 6 | Carry-once validated proposal (no re-freeze/re-hash) | Low–Med | Medium | Unit (proposal, decoder, hashing) | open |

Order: 1 → 2A → re-measure → 6 → 3-T1 → 2B → 4A → re-measure. Fixes 1 and
2A remove the noise that would otherwise confound the later measurements;
fix 6 is CPU-bound and scales with graph size, so it matters more for the
larger graphs that follow the tutorial.

## Working rules for every fix

- One fix per commit series; branch `claude/vibrant-euler-tora25` unless told
  otherwise. Run `scripts/branch-safety-check.sh --intent commit` before each
  commit and `--intent push` before each push.
- Read [CONTRIBUTING.md § Whole-tree gates](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit)
  before touching anything under `src/elspeth/web`. No new `getattr`, no
  wire-shape changes without updating the pinned templates.
- Tests: run to a log file, read the exit code, state scope. Fixes that touch
  SQL, sessions or coordination (fix 3) also need
  `pytest tests/ -m testcontainer -n 0`.
- Compare the keyless `elspeth-lints check --rules all --root src/elspeth`
  finding corpus before and after; never add aliases or padding to reduce it.
- Composer invariants hold throughout: nothing authors pipeline structure
  server-side; no tutorial-only paths. Fix 4 in particular must keep discovery
  on-demand through the provider's tools.
- Measure before claiming. Each fix has an "Evidence" line to fill with the
  instrument and its raw output; the measurement procedure is at the end.

---

## Fix 1 — Cache immutable catalog summaries

**Mechanism (verified).** `CatalogServiceImpl._catalog_schema`
(`src/elspeth/web/catalog/service.py:434`) calls `plugin_cls.get_config_schema()`
unconditionally. `self._schema_cache` is populated at construction but only
read by `get_schema` (`:244-246`); the listing path never consults it.
`_to_summary` (`:389-432`) re-runs pydantic `model_json_schema` (plus
per-call `TypeAdapter` builds for LLM transform/source), re-parses with
`_SchemaObject.model_validate`, and rebuilds a strict pydantic
`PluginSummary` on every `list_*` call. `_catalog_items`
(`src/elspeth/web/plugin_policy/availability.py:36-41`) enumerates all three
kinds before availability filtering. `RequestPluginSnapshotFactory.for_user_id`
is called per request from catalog REST, every composer session route, the
composer turn, session prep, validate, execute, readiness and the tutorial.

**Why caching is safe.** `_to_summary` reads nothing dynamic. Principal scope,
secret-store `has_ref`/`get_secret` fingerprints, profile registry state and
the HMAC generation key all enter in `build_plugin_snapshot`, after listing.
Plugin classes register once per process. Memory is bounded (one summary per
registered plugin, ~60).

**Change.** In `CatalogServiceImpl.__init__`, build the summary list once per
kind from `_schema_cache[...].json_schema`; have `list_sources` /
`list_transforms` / `list_sinks` return fresh `list(...)` copies of immutable
summaries. Do **not** cache `PluginAvailabilitySnapshot` or anything
downstream of `build_plugin_snapshot`. Do not add a second memo at the
authoring-aids layer; `build_planner_authoring_aids`
(`src/elspeth/web/composer/planner_authoring_aids.py:2655-2683`) already
memoizes per `snapshot_hash` (max 8, deep-copied on return) and is not the
problem.

**Hazards.**
- `PolicyCatalogView` has a per-instance `_full_items_cache`; tests pin
  `list_sources.call_count == 2`. Cache in `CatalogServiceImpl`, not the view,
  so those pins stay valid.
- Summaries must be immutable or copied on return; a caller mutating a shared
  object would leak across requests (the authoring-aids memo deep-copies for
  exactly this reason).
- If any plugin registration path runs after `CatalogServiceImpl`
  construction, the cache must be invalidated there. Verify there is none.

**Tests.** `tests/unit/web/catalog/`, `tests/unit/web/plugin_policy/`, plus a
new assertion that `get_config_schema` is invoked once per plugin per
`CatalogServiceImpl` across repeated `list_*` calls (positive), and a control
that an un-cached double still shows N calls (negative).

**Acceptance.** After warm-up, repeated listing performs no schema
regeneration; output is byte-identical to the uncached implementation (compare
`canonical_json` of the summaries); cross-user snapshots still differ where
policy differs; CPU per 60-plugin listing drops from ~120 ms to near zero.

**Evidence.** _(fill in: instrument, raw output, commit)_

Full verification notes were produced in lane state at
`.claude/lanes/perf-eval/01-catalog.md` (gitignored; regenerate if needed).

---

## Fix 2 — Validation ownership

**Mechanism (verified).** Four owners trigger the same `validate_state`:

- (a) `sessionStore.applyResolvedInterpretation`
  (`src/elspeth/web/frontend/src/stores/sessionStore.ts:2026-2054`) sets
  composition state, then at `:2053` calls `getExecutionStore().validate()`
  directly.
- (b) The composition subscription (`stores/subscriptions.ts:454-556`) queues
  auto-validation on a version change via `pendingValidateTarget` /
  `validateInflight` / `lastValidatedVersionBySession`; its sanctioned manual
  entry point `requestValidate()` (`:559-581`) documents that callers must not
  call `useExecutionStore.validate()` directly — which (a) does.
- (c) `useAuditReadinessSync` (`components/audit/useAuditReadinessSync.ts`)
  independently fetches `/audit-readiness` and already projects the snapshot's
  `validation_result` into `executionStore` (`:21-43`).
- (d) `ReadinessService.compute_snapshot`
  (`src/elspeth/web/audit_readiness/service.py:560-566`) calls
  `validate_state(...)` with the same arguments as the `/validate` route
  (`src/elspeth/web/execution/routes.py:979-985`). No server-side cache.

`/validate` has no `AbortController`; supersession is by
`validationRequestSeq` (`executionStore.ts:752`) and
`serializeReviewMutation` (`api/client.ts:123-153, 1359`) runs both POSTs
sequentially, so the discarded one still completes server-side. One Accept
therefore costs three full `validate_state` runs plus an `/interpretations`
refresh.

**Correction to the code's rationale.** The comment at `sessionStore.ts:2044-2051`
says a resolve "does not guarantee" a version bump. The backend
`resolve_interpretation_event` (`src/elspeth/web/sessions/service.py:5103-5106`)
always writes a new `composition_states` row with `version += 1`, and the
route returns it as non-nullable `new_state` (`sessions/schemas.py:665`). On
accept/amend the direct call is a pure duplicate. The genuine null case is the
opt-out button (`components/.../AcknowledgementStack.tsx:160`,
`onResolved?.(null, null)`), which mints no new state.

### 2A — Route the resolve through the queue (do first)

**Change.** Replace the direct call at `sessionStore.ts:2053` with a forced
queue entry (`{sessionId, version, force: true}`) — the same mechanism
`requestValidate` uses. `force` bypasses the same-version cache, so the
opt-out/null case still re-validates and the Run gate still clears. On
accept/amend the queue already holds the bumped version, so the entries
coalesce into one POST. `sessionStore` cannot import `subscriptions`
(circular; `subscriptions.ts:244-250`) — use the existing
`getExecutionStore()`-style indirection at `sessionStore.ts:44` or a small
shared `validationRequests.ts`.

**Tests to update.** `stores/sessionStore.test.ts:2749-2817`
(`describe("applyResolvedInterpretation")`, asserts the direct `validateMock`
call for both non-null and null) → assert the queue entry instead;
`stores/subscriptions.test.ts:855-1176, 1355-1476, 1549-1780` → add a
forced-null-resolve case; `components/chat/ChatPanel.test.tsx:2201-2213`
should keep passing unchanged.

**Acceptance.** Version-changing resolve → exactly one POST `/validate`.
Null resolve → exactly one POST, Run gate clears. Rapid successive resolves →
one in-flight at a time, no stale result applied. Session switch mid-flight →
no cross-session application (existing `:2750-2777` pin).

**Evidence.** _(fill in)_

### 2B — Share validation with readiness (decision needed first)

**Open question for the developer.** `/validate` runs the
`surface_pending_interpretation_reviews` repair pass
(`execution/routes.py:950-956`); `/audit-readiness` does not. Which direction
should the dedupe go?

- Option (i): when a readiness snapshot for the target version is in flight or
  cached, `fireValidateLoop` projects its `validation_result` and marks the
  version validated instead of POSTing. Loses the repair pass on that path.
- Option (ii): keep the forced POST on the resolve path and skip the
  readiness refetch when `/validate` has just completed for the same version.
  Keeps the repair pass; readiness panel updates one hop later.

Until answered, 2B does not start. Tests affected either way:
`components/audit/AuditReadinessPanel.test.tsx` (`:193, :439, :453, :545,
:586, :846`), `components/workspace/ArtifactWorkspace.test.tsx:1657-1675`,
`test/inlineSourceIntegration.test.tsx:151-160, 374-385, 525`,
`stores/auditReadinessStore.test.ts:73-222, 499-630`.

**Deferred (2C).** A short-lived backend memo on `ExecutionService` keyed by
`(state_id, user_id, binding_generation_fingerprint)` would collapse the
readiness recompute. Only pursue if profiling after 2A/2B still shows the
duplicate; `tests/unit/web/audit_readiness/test_service.py:766-787` pins
`validate_state.assert_awaited_once`.

**Evidence.** _(fill in)_

Lane notes: `.claude/lanes/perf-eval/02-validation.md`.

---

## Fix 3 — Operation stream round trips

**Mechanism (verified).** Producer loop
(`src/elspeth/web/sessions/routes/composer/operations.py:349-421`): 1.0 s
cadence, 10 s heartbeat; each iteration calls
`get_with_database_now` (`:355`). `BoundedComposerStreamResponse`
(`src/elspeth/web/composer_stream.py:218`, and once pre-header at `:173`)
calls `authorize()` before every send. `ComposerStreamAuthServices.authorize`
reads identity row (twice), token family, roles + clock, and the full session
row. The route-level authorize re-runs `get_with_database_now` (`:315`) and,
when a progress frame is pending, the five-statement progress read
(`:324-336`; `composer_progress_authority.py:442-462`).

`get_with_database_now` (`src/elspeth/web/coordination/composer_operation_authority.py:413-418`)
is two executes on one connection: `_read` then `database_now`
(`web/coordination/database_clock.py:66-70`, `SELECT clock_timestamp()`). It
selects every column except `request_json`, so `result_json`,
`result_sha256` and lease tokens are read on every poll and terminal rows
are re-hashed.

Static count per 1 s iteration on a running operation with progress: ~30
statements, 9–10 clock reads (≈⅓, matching the measurement). Two statements
are issued verbatim twice within the same frame (producer read ≡
authorize read). Clocks are sampled on 3–4 separate connections per frame.

**Correction to the profile's attribution.** The worker-side cutoff watch
(`src/elspeth/web/composer_watch_reads.py:228`) sleeps 0.25 s between
`get_with_database_now` reads — ~4/s, ~87 in the 21.7 s window — versus ~22
from the stream producer. Most of the "136 occurrences" is probably the
watch, not the stream. **Before choosing targets, count per caller** (see
measurement procedure).

No LISTEN/NOTIFY or version counter exists; only process-local
`asyncio.Event`s (`_local_cancels`, `_wake`, lease `_lost_event`).

### 3-T1 — Consolidate reads, no semantic change

**Changes.**
1. One-statement `get_with_database_now`: `SELECT <cols>, clock_timestamp()`
   on one connection. Time still comes from the database (ADR-047 honoured).
2. Narrow projection for the running-state poll (status, cutoff, lease epoch,
   cancellation — not `result_json`/`result_sha256`); keep the full read for
   terminal settlement.
3. One principal-authorization verb: identity, roles, token family and a
   3-column session read on a single connection with a single clock sample.
4. `get_session_for_stream` trimmed to the columns it uses; `connect()` not a
   transaction where no write follows.
5. Apply the same one-statement read to the 0.25 s watch loop.

**Hazards.**
- ADR-047 ([047-landscape-database-clock-authority.md](../architecture/adr/047-landscape-database-clock-authority.md))
  clock-provenance gates and ADR-048 coordination tokens: confirm the
  combined statement still satisfies the provenance check.
- Frozen writer inventory
  (`tests/unit/architecture/test_session_db_mutation_authority.py:5166-5174`)
  fingerprints `get_with_database_now`; any body change churns it and must
  be re-pinned honestly, not bypassed.
- `tests/unit/web/coordination/test_composer_operation_authority.py:1896-1913`
  monkeypatches `database_now`; the test needs a new seam if the clock call
  is folded into the row read.
- `tests/unit/web/sessions/test_composer_async_worker.py:480-615` reads the
  clock half of the tuple.
- SQLite cannot show PostgreSQL contention; testcontainer suites are
  mandatory, not optional, for this fix.

**Tests (inventory from lane notes §5).**
- Per-send auth: `tests/unit/web/test_composer_stream.py`,
  `test_composer_stream_live_auth.py`, `test_composer_stream_pool_capacity.py`,
  `tests/unit/web/sessions/test_routes.py:4640-4675` (source-string pins,
  negative control `auth.unchecked_authorize(`).
- Revocation: `tests/integration/web/test_composer_sso_stream_revocation.py`,
  `tests/testcontainer/web/test_cross_process_progress_postgres.py::test_existing_reader_rechecks_identity_after_revocation`,
  `tests/unit/web/auth/test_pipeline_user_authorization.py`,
  `test_routes.py::test_cancelled_auto_commit_persists_concurrent_trust_revocation`.
- Expiry: `test_composer_operation_authority.py::test_expired_job_refuses_business_write_at_database_clock`,
  `test_composer_stream.py::test_expiry_at_actual_frame_invocation_prevents_new_data_send`.
- Cancellation: `test_composer_stream.py::test_cancellation_resistant_send_retains_capacity_until_actual_completion`,
  `test_composer_stream_worker_custody.py`,
  `tests/unit/web/sessions/test_worker_owned_sql_cancellations.py`,
  `test_composer_operation_authority.py::test_cancel_refuses_business_but_sealed_failure_can_settle`.
- Fencing: `test_composer_operation_authority.py::test_real_schema_cross_session_and_cross_operation_bindings_are_rejected`,
  `test_composer_stream_live_auth.py::test_operation_from_another_owned_session_never_enters_stream_custody`,
  `tests/unit/web/sessions/test_composer_review_regressions.py:37-47`
  (fault injection on `get_with_database_now`).

**Acceptance.** Statements per delivered frame roughly halved with the
decision set unchanged; all revocation/expiry/cancellation/fencing tests
green on SQLite and PostgreSQL; a controlled-latency run (e.g. `tc qdisc`
or a proxy adding 20 ms) shows proportionally reduced frame latency.

**Evidence.** _(fill in)_

### 3-T2 — Snapshot-bound per-send authorization (ruling needed)

Dropping the authorize-side operation re-read in favour of the producer's
read from the same iteration is a semantic change. `composer_stream.py:72-73`
states "Every subsequent send requires a fresh authorization decision" and
`test_routes.py:4651` pins it. **Developer ruling required:** may "fresh"
mean "read earlier in the same ≤1 s iteration"? Do not fold into 3-T1.

### Deferred — change notifications

LISTEN/NOTIFY with bounded polling fallback is a multi-replica correctness
design (roadmap B.5), not a tuning change. Out of scope here.

Lane notes: `.claude/lanes/perf-eval/03-streaming.md`.

---

## Fix 4 — Provider context and reasoning

**Prompt layout (verified).** `pipeline_planner.py:3487-3500` builds three
messages: system = rendered skill (hash recorded as `composer_skill_hash`);
user 1 = `canonical_json({authoring_aids, schema_contract_evidence})`;
user 2 = `canonical_json(provider_request)` (intent, current state,
information manifest, conversation/recovery context). Byte ceilings bound the
reference block (`planner_authoring_aids.py:1141-1158`: 96 KiB schema
evidence, 48 KiB planner contract, 28 KiB discovery digest) and
`canonical_bytes_used` is recorded for schema evidence. The LLM-call ledger
(`composer/audit.py:442-448`) records provider totals only.

### 4A — Per-component accounting (do before any trimming)

**Change.** Record per-message byte length and, where the provider or a
tokenizer supports it, token count for each of the three components, plus
tool-definition size, in the existing per-call ledger. Additive field; no
behaviour change. Admit the new field in any closed export shape.

**Acceptance.** A planner call's ledger row shows the split; the sum
reconciles with the provider's `prompt_tokens` within tokenizer tolerance.

**Evidence.** _(fill in)_

### 4B — Reasoning effort

`composer_candidate_reasoning_effort` defaults to `"high"`
(`src/elspeth/web/config.py:318`). `apply_reasoning_kwargs`
(`src/elspeth/web/composer/reasoning.py:30-57`) sends it as OpenRouter's
native `reasoning: {effort}` object for `openrouter/` models (`:42-44`), as
LiteLLM `reasoning_effort` for other prefixed providers (Bedrock, Azure), and
**deliberately omits it for OpenAI-surface models** (bare aliases such as
`gpt-5.5`, or `openai/…`, `:45-56`) because LiteLLM would reroute
tool+reasoning calls to `/v1/responses`, which the gateway's `extra="forbid"`
contract does not serve (legacy id elspeth-9a46553771).

The test configuration for this work uses
`openrouter/deepseek/deepseek-v4.1-flash` (composer) and
`openrouter/z-ai/glm-5.3` (advisor), so the knob **is live** here: the
baseline's 8,213 reasoning tokens were produced under `candidate = "high"`.
A `"medium"` comparison is a one-line settings change
(`ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT`), but is only meaningful
with the quality measures below alongside latency.

Any tuning must measure proposal quality, extra tool calls, rejection rate
and recovery behaviour, not just latency.

### Context trimming (after 4A)

Only with 4A numbers in hand. Policy-aware, on-demand schema discovery must
remain a provider tool call; nothing moves server-side.

---

## Fix 5 — Memory

RSS 424 → 518 MiB over the flow does not prove a leak. Fix 1 removes the
largest transient allocator (repeated `model_json_schema`). After 1 and 2A
land: `tracemalloc` snapshots across several sessions then quiescence;
separate transient volume from retained objects; compare instrumented vs
minimally instrumented runs.

---

## Fix 6 — Carry a validated, immutable proposal once

**Mechanism (verified, `src/elspeth/web/composer/pipeline_proposal.py`).**
One `PipelineProposal.create(...)` call performs four full
validate/copy/freeze traversals of the same pipeline, six explicit
`canonical_json` serializations (eight counting the two inside
`stable_hash`), two `pipeline_draft_hash` computations and two
`project_composer_authority_payload` projections; `freeze_fields` adds a
fifth `deep_freeze` traversal. Counts are identical at 1, 50 and 300 nodes.
The chain:

1. `create` (`:619`) → `_validate_and_freeze_canonical_mapping`.
2. `create` (`:620`) → `pipeline_draft_hash` (`:553`), which freezes again
   (`:561`) and serializes for the hash.
3. Dataclass construction → `__post_init__` (`:587-588`) freezes a third
   time.
4. `__post_init__` (`:597`) recomputes `pipeline_draft_hash` → fourth
   freeze and another serialization.

`_validate_and_freeze_canonical_mapping` (`:231`) never short-circuits on
already-frozen input — it rebuilds every `MappingProxyType` /
`FrozenJsonArray` (`:172-212`) — and then serializes the result purely to
check `JSON_MAX_TOTAL_UTF8_BYTES` (`:253-254`), discarding the bytes.

Measured (isolated, 20 reps, no provider): ~0.9 ms at 781 B, 20.8 ms at
26 KB, 154 ms at 157 KB — about 1 ms per KB, CPU-bound. This does not alone
explain multi-second delays; it scales with graph size and with how often
proposals are reconstructed.

**Not redundant — keep.** The candidate path validates before *and* after
`canonicalize_authored_node_review_requirements`
(`pipeline_planner.py:3088-3099`). The second pass follows a real rewrite
(server-owned `id`/`status` filled on review rows) and runs with
`_interpretation_requirements_are_internal=True`, so it checks bytes the
first never saw. Likewise `from_dict` and `proposal_decoder.py:239` (the only
direct `PipelineProposal(` in `src/`) restore persisted/external data and
must keep full verification ([ADR-032](../architecture/adr/032-validate-by-trust-domain.md):
parse what we do not own; trust what we constructed).

**Provider-request re-processing (same class, separate fix).** Per planner
call the 223–310 KB payload is touched by `json.loads(canonical_json(...))`
for the call-input snapshot, the manifest's `canonical_json` + `stable_hash`,
the request-size `canonical_json`, and `capture_provider_context`. Most of
these are trust-boundary or audit work; the genuine redundancy is repeat
serialization of the *same* `messages`/`tools` snapshot. Treat as a
follow-on once the proposal path is fixed and 4A accounting exists. Not
instrumented live; read from code.

**Change.**
- In `create`: freeze once, hash the already-frozen pipeline (add a
  `pipeline_draft_hash` entry point or keyword that accepts pre-frozen
  input and skips the freeze), and drop the discarded
  `canonical_json(preimage)` where the bytes are only used for a size check
  that an earlier pass already performed.
- Construction from `create`: use a private in-module constructor that
  bypasses `__post_init__` re-verification for values `create` has just
  verified. Do **not** add a public "already validated" flag field — a
  flag on the dataclass can be forged by any caller; a module-private
  constructor cannot be reached through `from_dict` or the decoder.
- Carry canonical bytes, byte length and digest on the frozen object so
  downstream consumers (commit, audit, hashing) reuse them instead of
  re-serializing.
- Leave `from_dict`, `proposal_decoder.py` and every external/persisted
  entry on the full verification path.

**Tests.** `tests/unit/web/composer/test_pipeline_proposal.py`, the coalesce
and row-union hashing tests (grep `pipeline_draft_hash`), and
`proposal_decoder` tests pin current behaviour. Add: a call-count assertion
that one `create` performs exactly one freeze and one hash (positive) with an
unpatched control showing four/two (negative); a byte-identity assertion that
`draft_hash` and `canonical_json(proposal.pipeline)` are unchanged for the
existing fixtures; and a forgery test showing a hand-built
`PipelineProposal(...)` / `from_dict` with a wrong `draft_hash` is still
refused.

**Acceptance.** Freeze/hash counts per `create` drop to 1/1; draft hashes
byte-identical across the fixture corpus; the 157 KB case drops from ~154 ms
toward ~40 ms; the whole-tree gates (`tests/unit/architecture/`) unchanged.

**Evidence.** _(fill in)_

Lane notes: `.claude/lanes/perf-eval/06-proposal-revalidation.md`.

---

## Measurement procedure

Run before fix 1 and after each fix lands, same machine, same model, same
tutorial script, local PostgreSQL. Test models: composer
`openrouter/deepseek/deepseek-v4.1-flash`, advisor `openrouter/z-ai/glm-5.3`;
composition turns 10, discovery turns 24, timeout 240 s, rate limit 20/min.
The OpenRouter key is operator-supplied with a small manually reloaded credit
balance (~$10): check remaining credit before any run that drives the planner
and report low balance rather than letting a run fail mid-flow. Keys live only
in the gitignored `.env`; never in a tracked file or this document.

1. **Harness first.** Remove the ~30 s wait on the optional, absent proposal
   control before comparing end-to-end durations.
2. **SQL per caller.** Enable SQLAlchemy `before_cursor_execute` logging with
   a stack-tag (or `pg_stat_statements` plus application_name per task) so
   `get_with_database_now` calls can be attributed to stream producer,
   stream authorize, and watch loop separately.
3. **Catalog CPU.** Isolated benchmark: construct `CatalogServiceImpl`, call
   `list_sources/transforms/sinks` 50×, report CPU per iteration after
   warm-up. Target after fix 1: ≪10 ms.
4. **Validation count.** Browser network log: count POST `/validate` and GET
   `/audit-readiness` per user action (accept, amend, opt-out, import).
5. **Planner ledger.** Per-call token split from 4A once landed; until then,
   provider totals only.
6. **Memory.** `tracemalloc` as in fix 5.

Record raw output, not summaries, in each fix's Evidence line.

## Open questions for the developer

1. Fix 2B: which way does the validate/readiness dedupe go (keep repair pass
   on `/validate`, or project readiness result)?
2. Fix 3-T2: may the per-send authorization reuse the producer's read from
   the same iteration?
3. Fix 3: should the 0.25 s watch cadence itself be revisited, or only its
   query shape?

## Test environment (cloud session, set up 2026-11-08)

- `uv sync --all-extras` creates `.venv` (system Python 3.13); frontend built
  with `npm --prefix src/elspeth/web/frontend ci && … run build` on Node 22
  (pinned toolchain is 24; the build succeeded).
- **No Docker.** The `testcontainer`-marked suites cannot run here. A local
  PostgreSQL 16 cluster (`pg_ctlcluster 16 main start`) with role `elspeth`
  and databases `elspeth_sessions` / `elspeth_landscape` stands in for the
  server's own state stores; schema initialized with
  `elspeth doctor deployment --init-schema`. Contention proofs for fix 3
  still need a Docker host or CI's Testcontainer job.
- Server: `elspeth web --host 127.0.0.1 --port 8451` with `.env` providing
  `DEPLOYMENT_STATE_MODE=external-postgresql`, the two PostgreSQL URLs,
  `DATA_DIR`, `PAYLOAD_STORE_PATH`, the OpenRouter models and key, and
  `COMPOSER_PROMPT_GUARD_MODE=debug-when-unconfigured` (the documented
  development mode; records `debug_bypass`, unprotected). Local user `demo`.
- The doctor warns `composer_turn_budget_underfunded`: 240 s funds ~16 turns
  against 34 configured. Left as supplied; revisit if runs time out.
- Network: this session was moved onto a personal environment allowing
  `openrouter.ai`; the Default environment was not changed.
- `scripts/branch-safety-check.sh` and `scripts/full-suite-gate.sh` are the
  canonical pre-commit/pre-merge instruments; use them, not raw commands.
  The PreToolUse guard refuses `pkill -f`; stop the server with `kill <pid>`.
