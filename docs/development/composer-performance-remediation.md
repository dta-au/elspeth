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

## Governing rule

Every fix below is an instance of one rule: **verify external data when
taking ownership, retain the verified immutable representation, and
recompute only facts whose inputs changed.** Each layer today reconstructs
or re-verifies because its interface does not carry the already-verified
object forward. Authorization, blob availability, review state and
transaction fences have different lifetimes from graph structure and static
schemas; cache by lifetime, never across one.

Corollary: `MappingProxyType` is not proof of ownership or immutability —
its backing dict can still change elsewhere. "Skip freezing if it is already
a proxy" is a correctness bug, not an optimization. Ownership is proved by
*who constructed it* (a module-private constructor reachable only from the
verifying path), not by the type of the container.

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
| 7 | Approval binding reuses the cached runtime manifest | Low | Low | Unit (approval, landscape) | open — governance-on only |
| 9 | Drop the executor's duplicate input hash | Low | Low | Engine unit + telemetry tests | open |
| 10 | Load `chat_messages.tool_calls` once per list, filter by id | Low–Med | Medium | Reviewed-writer recipe re-pin; sessions unit | open |
| 11 | Memoize tool-definition credential scan by tools hash | Low | Low | Unit (provider_context) | open |
| 12 | Cache `canonical_set_pipeline_schema()` per process | Low | Low | Unit (schema_contract, planner) | open |
| 13 | Review-card publication: prepare once per candidate, batch fenced reads | Medium | Medium–High | Sessions + interpretation unit; recipe pins | open |
| 14 | Preserve state identity: single restore, no-op reconcile returns same object, hash once | Low–Med | Medium | Proposal/decoder/sessions unit | open |
| 15 | Candidate build: drop the third full walk on the identical reconciled object | Low–Med | Low | Planner unit | open — commit rebuild stays |

Externally reported items verified here and left pending (lane report
`.claude/lanes/perf-eval/09-pending-p11-p16.md`); P-11/12/13 were promoted
to fixes 13/14/15 below:

| Pending | Verified mechanism | Status |
|---|---|---|
| P-14 Provider-request preparation | JSON round trip 5.5 ms, manifest 12.5 ms, size serialization 4.8 ms, private capture 62 ms (1.56 MiB transient), hash pair 4.8 ms, retained-payload validation ~15 ms; schema double-build is fix 12 | Follow-on to fixes 6, 11, 12 and 4A |
| P-15 Application fingerprint | **Corrected:** runs per run admission and per restore (a fresh run pays it twice), not at process launch. 990 files / 23,777,024 B confirmed; ~0.085 s hashing + ~0.127 s dependency scan. In a built dev tree the glob also hashes 6 `.py` files under `src/elspeth/web/frontend/node_modules/` — a correctness smell (the fingerprint varies with a frontend install). No test pins the computation | Fix: cache per process keyed on tree mtime/size manifest with explicit invalidation **after** a ruling on whether in-place edits must be detected per run; exclude `node_modules` regardless |
| P-16 Recovery envelope | **Downgraded:** 3 parses per restore (4 per fresh run) at ~0.1 ms each — negligible. The real redundancy is retained inputs and blobs re-hashed twice on restore (`envelope.py:574/577` vs `:380/390`). The restore parse at `:361` is the trust-boundary one | Low priority; fold the double hash into fix 9's class |

From the in-repo systems scan (`.claude/lanes/perf-eval/07-systems-scan.md`;
per-turn counts read from code, costs from isolated timing where stated,
no live run). Pattern G — synchronous work on the event loop — was not in
the original list:

| Pending | Pattern | Mechanism | Scale / status |
|---|---|---|---|
| S-3 Event-loop stalls | G | Plugin snapshot builds (`composer_turn.py:931`, `composer/service.py:1228`, `routes/_helpers.py:299` via GET /state) and ~30–45 ms of per-provider-call preparation run synchronously on the loop, stalling stream frames and other sessions | Low risk: move to a worker thread. Fix 1 shrinks the snapshot cost first |
| S-4 Prompt guard re-screens history | D | `prompt_guard.py:195`, `prompt_guard_clients.py:137`: all untrusted history re-screened every provider call in sequential 10,000-char batches, new HTTP client per call | Not measured (baseline ran guard bypassed). Client reuse is low risk. **Verdict caching needs a developer ruling** |
| S-5 Full transcript read per turn | D/F | `messages.py:118` reads every row unbounded and slices in Python; frontend `fetchMessages` (`client.ts:841`) re-reads the whole session per page | Linear per turn, quadratic over a session |
| S-6 Discovery tools re-validate unchanged state | A | `tools/_common.py:1281`, `:3829`; `_dispatch.py:923`, `:1039` — 2–3 validations of the same state per tool call, up to 24 calls/turn | Low–medium |
| S-7 Plugin snapshot rebuilt 2+K× per turn | C | `composer_turn.py:931`, `service.py:1228`, plus once per interpretation event at `sessions/service.py:4996-5018`, each re-serializing ~120 schemas | **Needs a ruling**: sharing one snapshot across a turn changes authorization freshness within that turn |
| S-8 Advisor transcript snapshot | D/A | `advisor_transcript.py:769` re-parses, re-hashes and re-reads blobs for the whole history 2–6×/turn | Medium; the blob re-check may be deliberate |
| S-9 Head graph triple decode | A | Decoded 3×, checked 1–2× per read | 10–30 ms at 30–100 nodes |
| S-10/11 Accept re-checks own arguments; serialize-then-hash twice | A | Proposal accept re-verifies arguments it just produced (10–81 ms); same value serialized then hashed separately back-to-back | Fold into fix 6 |
| S-12 Post-turn GET fan-out | F | Frontend fires 6–7 GETs after every turn, beyond fix 2's validate/readiness pair | Extend fix 2B's coordinator |

Order: 1 → 2A → re-measure → 10 → 6 (+S-10/11) → 14 → 15 → 11 → 12 →
3-T1 → 13 → 2B (+S-12) → 4A → 7 → 9 → re-measure; other pending items slot
in once verified and, where marked, ruled on. Fix 13 sits after 3-T1 because
it is the widest change to session-write transactions and benefits from
fixes 1, 14 and 15 having shrunk the per-card cost first. Fixes 1 and 2A remove the noise that would otherwise confound
the later measurements. Fix 10 comes next because its cost scales with
history × proposals rather than being a fixed per-request charge; fix 6 is
CPU-bound and scales with graph size, so both matter more for the larger
graphs that follow the tutorial. Fix 7 only fires with workflow governance
on; fix 9 is engine-side and scales with rows × retries.

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
- Do **not** short-circuit `_validate_and_freeze_canonical_mapping` on
  `isinstance(value, MappingProxyType)`: a proxy's backing dict is still
  mutable by whoever holds it (see Governing rule). The only trusted input
  is the object this module froze itself, on the path it controls.
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

## Fix 7 — Approval binding rebuilds the runtime manifest

**Mechanism (verified).** `runtime_val_manifest_sha256()`
(`src/elspeth/web/coordination/approval_authority.py:114-120`) calls
`prepare_for_run()` then `build_runtime_val_manifest()` uncached on every
approval binding — measured ~0.35 s per call here (profile: ~0.7 s). The
Landscape run-lifecycle repository already caches the identical frozen
manifest JSON (`_cached_frozen_runtime_val_manifest_json`, keyed on
`runtime_val_registry_fingerprint()`) for `begin_run`, returning in 22–58 µs
warm; both paths hash the same bytes (equality measured). The cache is
private to Landscape, which is why approval binding cannot reach it.

**Scope.** Only runs when `workflow_governance == "on"` (default `"off"`);
irrelevant to the tutorial, significant for governed deployments.

**Change.** Move the fingerprint-keyed cached accessor into
`src/elspeth/contracts/runtime_val_manifest.py` and call it from both
`begin_run` and `runtime_val_manifest_sha256`. Keep the uncached rebuild in
`resume.py:640` — it is a deliberate drift check and must see the live tree.

**Tests.** Approval-authority unit tests, Landscape run-lifecycle tests, and
a new assertion that the two callers agree byte-for-byte and that a changed
registry fingerprint invalidates the shared cache.

**Evidence.** _(fill in)_

---

## Fix 9 — Executor and audit repository both hash every row

**Mechanism (verified, incidental duplication).** The transform executor
hashes the input (`engine/executors/transform.py:880`) and the output
(`:737`); the Landscape repository re-hashes both independently
(`node_states.py:154` and `:610`). Nothing compares the pairs — the
repository is never handed the executor's value — so this is not an
integrity cross-check; the executor's hashes feed telemetry and one test.
(Aggregation differs: `execution_repository.py:1018` *does* verify a hash it
is given; leave that alone.) Cost scales with rows × payload × retries.

**Change.** Reuse the `NodeStateOpen.input_hash` the repository already
computed instead of hashing input again in the executor. **Keep the
executor's output hash**: it converts NaN/Infinity into a routed Tier-2
violation before `guard.complete`; removing it would turn that failure into
a crash. Net: one of four hashes removed per attempt.

**Tests.** Engine executor unit tests, telemetry assertions that consume
`input_hash`, retry tests under `tests/unit/engine/`. Add a negative
control showing the NaN/Infinity path still routes as a violation.

**Evidence.** _(fill in)_

---

## Fix 10 — Proposal verification loads the whole `tool_calls` history

**Mechanism (verified).** Dispatch custody verification runs
`select(chat_messages_table.c.tool_calls).where(session_id == sid)` with no
`tool_call_id` or role filter, then matches in Python
(`sessions/proposal_authority.py:327-335`
`_persisted_pipeline_dispatch_content_hashes`; `sessions/service.py:8117`
dispatch recovery). `tool_calls` is a JSON column on `chat_messages`, not a
table, and no index helps: `tool_call_id` is populated only on tool rows
while dispatch ids live inside JSON on audit rows. The proposal-list path
(`_list_composition_proposals_on_connection`, `service.py:4705-4710`) calls
`_verify_pipeline_lifecycle_authority` per committed or dispatch-bearing
rejected proposal, so one list request does one full history load per such
proposal. Call sites: `service.py:3664, 4144, 4598`. Cost ∝ history size ×
proposal count; the profile measured 1.18 MB of history against a 36 KB
state row (the 1.18 MB figure and the "retained provider-request snapshot"
attribution were not re-verified here).

**Change (two steps, both preserve the duplicate/mismatch custody checks).**
1. Load the session's `tool_calls` rows once per list call and group
   envelopes by `tool_call_id`; pass the grouping into
   `_persisted_pipeline_dispatch_content_hashes` and the recovery lookup
   instead of re-querying per proposal.
2. Add a superset prefilter the custody check can trust (e.g. `role =
   'audit'`, or a persisted `dispatch_tool_call_id` column on the rows that
   carry envelopes, with a migration under the 0.8.7+ compatibility
   posture) so a single proposal's verification reads only its candidate
   rows. Step 2 needs a schema decision; step 1 does not.

**Hazards.** `reviewed-writer-recipes.json:136` pins the AST of
`_persisted_pipeline_dispatch_content_hashes` — any edit requires the recipe
regenerated honestly, not bypassed. The duplicate-envelope check (`len(...)
!= 1` at `:4598`) and the content-hash mismatch checks must produce the same
verdicts on the same data before and after.

**Tests.** `tests/unit/web/sessions/` tests for proposal listing, dispatch
custody and recovery; add a statement-count assertion (SQLAlchemy event
listener) that listing N committed proposals issues one `chat_messages`
read, plus a negative control on the pre-fix shape.

**Evidence.** _(fill in)_

Lane notes: `.claude/lanes/perf-eval/08-manifest-rowhash-toolcalls.md`.

---

## Fix 11 — Tool-definition credential scan repeated per provider call

**Mechanism (verified).** `capture_provider_context`
(`provider_gateway.py:765`, called from `pipeline_planner.py:3755`) runs
`_check_tools` (`src/elspeth/web/composer/provider_context.py:204-212`) over
the planner's fixed tool definitions on every provider call, scanning them
for credential-shaped content. The tool set is process-invariant for a given
discovery policy and dialect. Measured ~20 ms per call; up to ~0.7 s across
a compose-loop turn.

**Change.** Memoize the tools verdict by a hash of the canonical tool list
(bounded dict, process lifetime). **Keep scanning `messages` on every
call** — they carry user and provider content and are the actual trust
boundary.

**Tests.** `tests/unit/web/composer/` provider-context tests; add a
positive (second call with identical tools performs no scan) and a negative
(a tool list containing a credential-shaped string is still refused, cached
or not).

**Evidence.** _(fill in)_

---

## Fix 12 — `canonical_set_pipeline_schema()` regenerated per call

**Mechanism (verified).** `canonical_set_pipeline_schema`
(`src/elspeth/web/composer/tools/schema_contract.py:403-413`) deep-copies
the registered `set_pipeline` schema and augments it with runtime naming
constraints on every call; there is no cache in the module. It is called
twice back-to-back at `pipeline_planner.py:3625-3626`, again after each
discovery batch (`:5125`), at `:1595`, `:2745`, and on every schema
rejection (`:4487`, where a `Draft202012Validator` is also rebuilt around
it). Measured ~4.6 ms per call, ~15 ms per planner call.

**Change.** Build the augmented schema once per process (the registered
schema and the naming constraints are both static) and return a deep copy
— callers currently rely on an isolated copy. Cache the compiled
`Draft202012Validator` alongside it. If profiling shows the deep copy
itself is the cost, return an immutable view instead and audit the callers
for mutation.

**Tests.** `tests/unit/web/composer/tools/` schema-contract tests; add a
call-count assertion and a byte-identity check against the uncached result.

**Evidence.** _(fill in)_

---

## Fix 13 — Review-card publication repeats shared preparation per card

**Mechanism (verified).** Publishing R interpretation cards for one
candidate re-prepares, per card, the principal/plugin snapshot, inline
preflight config, blob snapshot and validation inputs
(`sessions/service.py:4996-5019`), and inside the write transaction
re-selects the anchor and head `composition_states` rows per card
(`repository.py:1073`, `:1085`) even when they are the same row. Existing
pending cards at the same (node, kind) are loaded *with their surfacing
graphs* before term filtering, giving `R(R−1)/2` lookups for R terms at one
node/kind (derived from code; the complex graph's five cards sit in
different groups, so that run was linear). The prepared validator is only
consumed on the review-disabled arm (`pending_interpretation.py:2125`).

**Change.** Prepare shared candidate context once per publication (snapshot,
inline config, blob snapshot, validation inputs); carry a transaction-local
map `state_id → verified row + derived graph`; filter terms before loading
surfacing graphs. Advance the shared context only when a card actually
changes state. Keep per-card provenance and authorization checks.

**Hazards.** Widest transaction change in the set; writer-recipe pins on
the interpretation write path (check `reviewed-writer-recipes.json`);
SQLite vs PostgreSQL fence semantics — needs the Testcontainer job.

**Tests.** `tests/unit/web/sessions/` interpretation publication tests;
add a statement-count assertion (N cards → one anchor/head read) and a
quadratic-shape negative control with R terms at one node/kind.

**Evidence.** _(fill in)_

---

## Fix 14 — Preserve state identity across restore / reconcile / hash

**Mechanism (verified, measured).** `composition_content_hash()` memoizes
correctly on the state instance (0.0007 ms on a hit) but ~10 call sites
discard the instance and rehydrate from the row. Specific duplicates:
`owned_composition_state_safe_sources` (`pipeline_proposal.py:528`)
restores the authority, then calls
`owned_composition_state_execution_arguments` (`:507`) which restores it
again; `read_state()` is called twice for the same record
(`sessions/service.py:6956`, `:6994`); reconcile returns a new object
unconditionally even on no change (memo lost; ~0.39 ms replacing all node
option objects); the audit-payload hash is computed twice in proposal
restore (`proposal_decoder.py:186`, `:258-261`) and a third time on list
(`service.py:4712`).

**Change.** Thread the restored state through instead of re-restoring;
make reconcile return the input object when nothing changed (identity, so
the memo survives); compute the audit-payload hash once in the decoder and
carry it on the restored proposal. Keep every external/persisted entry
(`from_dict`, decoder) on the full verification path (ADR-032).

**Tests.** `tests/unit/web/composer/test_pipeline_proposal.py`,
proposal_decoder tests, sessions list/read tests; add identity assertions
(`reconcile(x) is x` on no change; one restore per safe-sources call).

**Evidence.** _(fill in)_

---

## Fix 15 — Candidate build walks the same reconciled object twice

**Mechanism (verified, measured).** One candidate build performs three
full-graph validations plus one probe per source and per transform
(6.3 / 20.7 / 68.0 ms at 1 / 10 / 40 nodes). Walks 2 and 3 receive the
*same* reconciled state object (same `id()`); walk 1 extracts
mutation-blocking invariants from the pre-reconcile state. Only the
trained-operator path was measured.

**Keep:** `prepare_pipeline_commit` builds a dry-run candidate and the
audited dispatch builds again; the two are compared —
`candidate_hash != proposed_content_hash` → `AuditIntegrityError`
(`pipeline_commit.py:477-478`) and `executor_hash` bound into the dispatch
(`:557-558`). That is an integrity contract, not redundancy; a single-build
design needs an explicit replacement binding before it can be considered.

**Change.** Reuse walk 2's result for walk 3 when the object identity is
unchanged and no policy lowering intervened; policy lowering that changes
the executable graph still validates the changed representation.

**Tests.** Planner candidate-build tests under `tests/unit/web/composer/`;
add a walk-count assertion with identity check and a negative control where
lowering changes the graph and a third walk is still performed.

**Evidence.** _(fill in)_

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
4. S-7: may one plugin-availability snapshot be shared across a single
   composer turn (including the interpretation events it writes), or must
   each write re-read principal/credential state?
5. S-4: may prompt-guard verdicts for unchanged history segments be cached
   within a session, or must every provider call re-screen all history?

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
