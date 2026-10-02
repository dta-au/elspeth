# Seam re-survey: the composer service after the owner extraction

Read-only re-survey for re-basing the composer async-operations plan under the 2026-09-28 rulings
(`panel-2026-09-28/RULINGS.md`, `RECOMMENDATION.md` B′).

- **Tree:** `release/0.8.1` at `1effedab2` (`git rev-parse HEAD` printed
  `1effedab2e0af7e09a5e8b30c66bc46ceaa130d5` at the start of the survey; HEAD did not move, so no anchor
  needs a HEAD delta).
- **Old baseline:** `findings/` and `contract.md` were measured at `d479eb2b4`. They are used here only as a
  map of what to re-check.
- **Commits in this seam since then:** `fab986dc9` (advisor context and verdict policy), `ed84adf51`
  (admission and interpretation owners), `166a83620` (provider gateway), `a11977d79` (advisor checkpoint
  owner), `c38d50da6` (application owners and dependency inversion), `01d96af9a` (first-run tutorial Build
  moved to freeform Composer), `7001600fe` (Guided Composer removed), and `a375d7f13` (gateway sidecar
  upstream timeout default 300 s). The list comes from `git log --since=2026-09-26` over
  `src/elspeth/web/composer`, `config.py` and `sessions/routes`.

**Instrument caveat.** Partway through the survey the Bash tool became unavailable: the auto-mode
classifier returned no verdict seven times in a row. Four searches therefore ran before the outage, and
their raw output backs the claims marked **[grep]**:

- the `composer_timeout_seconds` / `_timeout_seconds` reader inventory;
- the `attach_llm_calls` sites;
- the `deadline` sites in `service.py`;
- the gateway env-var mirror search.

Every other claim comes from a direct `Read` of the cited lines. After the file was first written, one
batched read-only search ran successfully. Its results are folded in below and marked **[grep-2]**:

- `audit_only`;
- `ComposerAdmissionRefused(`;
- `Depends(_track_compose_inflight)`;
- `plan_guided`;
- the advisor `deadline` sites;
- the settlement callers;
- `add_messages_atomic`;
- the tutorial `sendMessage` call;
- `git rev-parse HEAD`, which again printed `1effedab2…`.

**Instrument controls for [grep-2].** In the same invocation, the patterns with known positives did match:

- `ComposerAdmissionRefused(` matched its class at `protocol.py:457`;
- `Depends(_track_compose_inflight)` matched `messages.py:142`;
- `settle_…(` matched the definitions.

A zero result for `audit_only` or `plan_guided` from that same run is therefore a real absence, not a
broken instrument. Anything still marked **NOT MEASURED** needed a search that did not run.

---

## 1. Current facts for this seam

### 1.1 Where `compose()` lives, and its signature

The implementation is `ComposerServiceImpl.compose` at `src/elspeth/web/composer/service.py:898-912`.
The protocol method is identical, at `src/elspeth/web/composer/protocol.py:1622-1636`:

```python
async def compose(
    self,
    message: str,
    messages: list[ComposerHistoryMessage],
    state: CompositionState,
    session_id: str | None = None,
    current_state_id: str | None = None,
    user_id: str | None = None,
    progress: ComposerProgressSink | None = None,
    user_message_id: str | None = None,
    session_operation_context: SessionOperationContext | None = None,
    completion_gates: CompletionGateFacts | None = None,
) -> ComposerResult:
```

- There is **no budget, deadline or timeout parameter.**
- The parameters are positional-or-keyword, not keyword-only.
- Entry order inside `compose()` (`service.py:932-948`) is:
  1. the availability check;
  2. SOL shape checks (`:934-940`);
  3. `await self._chargeable_admission.require(session_operation_context)` (`:942`);
  4. `with composer_quota_scope(...)` (`:943`);
  5. **only then** `deadline = asyncio.get_event_loop().time() + self._timeout_seconds` (`:944`).

  The admission round-trip therefore sits outside today's compose clock.
- The surface split is at `:969-1023`:
  - an empty state with explicit-mutation intent and a session and user message goes to
    `self._planning_application._plan_and_stage_empty_pipeline(...)` (`:994-1007`), **which is not passed
    `deadline`**;
  - everything else goes to `self._compose_loop(..., deadline, ...)` (`:1008-1023`).
- Protocol-level only: the `ComposerService` Protocol (`protocol.py:1612-1680`) now declares only
  `compose` and `explain_run_diagnostics`. The guided planner methods are gone from the Protocol (Read of
  the whole Protocol body). **[grep-2]** `grep -rn "plan_guided" src/elspeth --include=*.py` finds
  **0 hits**, so no `plan_guided_*` symbol survives in `src/`.

### 1.2 Where the wall-clock deadline is computed and consumed [grep]

`ComposerServiceImpl.__init__` sets `self._timeout_seconds = settings.composer_timeout_seconds` at
`service.py:574`. Its readers are:

| Site | What it does |
|---|---|
| `service.py:771` | `_run_one_turn_for_test` computes its own `deadline=` (a test driver) |
| `service.py:885` | `explain_run_diagnostics` calls `self._provider_gateway._call_text_llm_with_audit(messages, timeout=self._timeout_seconds, recorder=recorder)`, a **synchronous consumer** |
| `service.py:944` | `compose()` computes the compose deadline |

`deadline` is threaded through the loop:

- `_compose_loop` (`:1015`);
- the tool-batch context `deadline=deadline` (`:1345`);
- `_call_llm_before_deadline` (`:1178`, `:1725`);
- the advisor early checkpoint `_maybe_run_early_checkpoint(..., deadline=deadline, ...)`
  (`:2258-2267`).

Per provider call, `_call_llm_before_deadline` (`service.py:2577-2702`) computes
`remaining = deadline - loop.time()` (`:2629`) and calls
`_provider_gateway._call_llm_with_audit(messages, tools, timeout=remaining, recorder=recorder)`
(`:2641-2646`). The outcomes are:

- `remaining <= 0`, `TimeoutError`, or an `OpenAIError` that `classify_provider_failure` labels
  `kind == "timeout"` each become `ComposerConvergenceError.capture(budget_exhausted="timeout", ...)`
  (`:2630-2639`, `:2647-2656`, `:2681-2692`).
- A retryable provider error retries only while `remaining_after_error > delay_seconds` (`:2699-2702`).

### 1.3 `PlanningApplication`, and how the empty-pipeline planner gets its timeout

- `class PlanningApplication` is at `src/elspeth/web/composer/planning_application.py:216`. It was
  extracted in `c38d50da6` and is constructed once per service at `service.py:678-697`.
- It keeps **its own copy** of the budget: `self._timeout_seconds = settings.composer_timeout_seconds`
  (`planning_application.py:263`).
- The planner signature is at `planning_application.py:656-671`:

  ```python
  async def _plan_and_stage_empty_pipeline(self, *, message: str, messages: list[ComposerHistoryMessage],
      state: CompositionState, session_id: str, current_state_id: str | None, user_id: str | None,
      session_operation_context: SessionOperationContext | None = None, progress: ComposerProgressSink | None,
      user_message_id: str, recorder: BufferingRecorder, plugin_snapshot: PluginAvailabilitySnapshot,
      policy_catalog: PolicyCatalogView) -> ComposerResult
  ```

  It has no budget parameter.
- It passes `PlannerModelConfig(..., timeout_seconds=self._timeout_seconds, ...)` (`:731-755`, the field at
  `:737`) to `plan_pipeline`.
- `PlannerModelConfig.timeout_seconds: float` is declared at `pipeline_planner.py:616`, and
  `__post_init__` requires a finite positive value (`:711-714`, raising `TypeError`/`ValueError`).
- `plan_pipeline` starts **its own clock**:
  `deadline = asyncio.get_running_loop().time() + model_config.timeout_seconds` (`pipeline_planner.py:3093`).
  Each sync step is `asyncio.wait_for(run_sync_in_worker(...), timeout=remaining)` (`:3095-3102`). Each
  provider attempt is `asyncio.wait_for(model_config.completion(**kwargs), timeout=remaining)`
  (`:3359-3361`, `:3381`). Timeouts become
  `PipelinePlannerError("planner wall-clock budget exhausted", code="TIMEOUT")` (`:3097`, `:3102`, `:3362`,
  `:3425`).
- **Consequence:** on the planner surface the effective budget is the full `composer_timeout_seconds`,
  measured from `plan_pipeline` entry. That is *after* `_planner_preview_preflight` and
  `get_composer_preferences` (`planning_application.py:682`, `:695-702`), and independent of `compose()`'s
  `deadline` at `service.py:944`. Two clocks exist per request.
- The planner's lifecycle adapter assumes the HTTP envelope already exists. Its docstring at
  `planning_application.py:272-297` says: "HTTP callers have completed rate limiting and entered their
  in-flight and disconnect scopes before invoking `compose`". The adapters are no-ops (`:282-290`).

### 1.4 Provider gateway, and how the sidecar's `a375d7f13` interacts with the budget

There are two different things called "gateway".

1. **The in-process `ProviderGateway`** (`src/elspeth/web/composer/provider_gateway.py:364-383`,
   extracted in `166a83620`).
   - `_call_llm_with_audit(self, messages, tools, *, timeout: float, recorder: BufferingRecorder | None)`
     (`:440-447`) bounds the call with `asyncio.wait_for(self._call_llm(messages, tools), timeout=timeout)`
     (`:460`).
   - `_call_text_llm_with_audit(self, messages, *, timeout: float, recorder)` (`:530-536`) does the same
     at `:546`.
   - The LiteLLM kwargs builder `build_composer_loop_request_kwargs` (`:307-341`) sets
     `num_retries=0, max_retries=0` and **no `timeout` kwarg**. The only per-call bound is therefore the
     caller-supplied `asyncio.wait_for` timeout.
   - `_litellm_acompletion` (`:344-361`) is shared by the loop, the planner
     (`completion=provider_gateway._litellm_acompletion`, `planning_application.py:732`) and the advisor
     (`advisor_checkpoint.py:370-373`).
2. **The LLM gateway sidecar** (`gateway/src/elspeth_llm_gateway/`). This is what `a375d7f13` changed.
   - `GatewayConfig.request_timeout_seconds: float = 300.0` (`gateway/src/elspeth_llm_gateway/core/config.py:132`).
   - The loader default is `request_timeout_seconds = 300.0` (`:360`), overridable by
     `ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS` (`ENV_PREFIX` `:33`, key `:81`, parse `:359-367`).
   - It is applied as the httpx request timeout on the upstream POST
     (`core/transport.py:164-170`, `timeout=self._config.request_timeout_seconds`), and on the OAuth
     token call (`core/oauth.py:151`).
   - An `httpx.TimeoutException` becomes `GatewayError(GatewayErrorCode.UPSTREAM_TIMEOUT)`
     (`transport.py:172-177`).

**How they interact.** There is no code coupling. The sidecar knows nothing about
`composer_timeout_seconds`, and the composer knows nothing about the sidecar timeout.

- When a deployment points `composer_endpoint_base_url` at the sidecar, each provider call is bounded by
  **min(the caller's `remaining`, the sidecar's httpx timeout of 300 s, applied per httpx phase)**.
  Before `a375d7f13` the sidecar's 60 s was the binding cap, which is the 504 the commit message
  describes.
- Today every composer budget is at most the transport ceiling minus headroom (`config.py:1201`), and
  ECS ships 840 s (`variables.tf:624`). So the sidecar's 300 s can already bind below the composer budget
  on ECS if the sidecar is in path. Whether any shipped profile routes the composer through the sidecar
  is **NOT MEASURED**.
- The mirror search
  `grep -rn "ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT\|LLM_GATEWAY_REQUEST_TIMEOUT\|REQUEST_TIMEOUT_SECONDS"`
  over `deploy docs gateway` (tf/bicep/yaml/yml/md/env*/json/sh) **[grep]** matched only
  `docs/plans/2026-09-13-kubernetes-and-identity/K5-acceptance-probes.md:549,574`
  (`INGRESS_REQUEST_TIMEOUT_SECONDS`). That line is the positive control for the pattern. **No deployment
  mirror or doc sets or names the sidecar's request-timeout env var.** Its value is the code default
  everywhere.
- **Coincidence, not coupling:** 300 s also equals `_DEFAULT_COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`
  (`config.py:74`).

**The future remaining-budget parameter.** The worker will pass `remaining = deadline_at − now`. It flows
to `asyncio.wait_for(timeout=remaining)` per call, both in the loop (`service.py:2641-2646`) and in the
planner (`pipeline_planner.py:3381`). Once the async cutover lets `composer_timeout_seconds` exceed the
transport ceiling, the sidecar's 300 s becomes the binding per-call cap whenever `remaining > 300`.

- How a sidecar `UPSTREAM_TIMEOUT` response surfaces through LiteLLM into `classify_provider_failure`
  (a `timeout` kind, which means convergence 422, or a retryable or non-retryable API error) is
  **NOT MEASURED**.
- If it lands as `kind == "timeout"`, the 422 body's `timeout_seconds` (`_helpers.py:3223`) would report
  the configured compose budget for a cut made by a 300 s per-call cap. That is misleading under D10.

### 1.5 Admission owner (`ComposerAdmissionRefused`)

- `ComposerChargeableAdmission` is at `src/elspeth/web/composer/chargeable_admission.py:16-37`
  (extracted in `ed84adf51`).
- `require(session_operation_context)`:
  - raises `ComposerAdmissionRefused("Composer admission requires session authority.")` when the sessions
    service or context is absent (`:24-25`);
  - raises `TypeError` / `ValueError` for a wrong context type or kind (`:26-29`);
  - calls `sessions_service.assess_chargeable_operation(..., operation=ChargeableOperation.COMPOSER)`
    (`:30-33`);
  - raises `ComposerAdmissionRefused(f"Composer admission refused: {reason}.")` (`:37`), or a Tier-1
    `AuditIntegrityError` when a refusal has no reason (`:35-36`).
- `ComposerAdmissionRefused` is imported from `elspeth.web.composer.protocol` (`chargeable_admission.py:10`;
  `messages.py:9`; `compose.py:9`).
- Call sites read:
  - `compose()` at `service.py:942`, before the deadline;
  - `explain_run_diagnostics` at `service.py:868`;
  - `PlanningApplication` holds the same instance (`service.py:681`);
  - `AdvisorCheckpointOwner` holds it (`service.py:657`).
- Route mapping: 403 `{"error_type":"composer_admission_refused","failure_code":"admission_refused","detail":str(exc)}`
  at `messages.py:856-870` and `execution/routes.py:1494-1498`. Recompose catches it at `compose.py:603`.
- **[grep-2]** `class ComposerAdmissionRefused(ComposerServiceError)` is at
  `src/elspeth/web/composer/protocol.py:457`.
  - The **only** raisers in `src/` are `chargeable_admission.py:25` and `:37`. The search matched 3 lines:
    the class and the two raises.
  - Because it subclasses `ComposerServiceError`, its arm must precede the generic `ComposerServiceError`
    arm. It does in both places: `messages.py:856` before `:871`, and `execution/routes.py:1494` before
    `:1499`.
  - The worker's error projection (T09) must preserve that precedence, or the 403 collapses to the 502
    `composer_error`.
- `ChargeableAdmissionRefused` (`elspeth.contracts.chargeable_admission`) is a separate class caught at
  `messages.py:847-855` and `compose.py:594`. It is re-raised with `attach_llm_calls` at
  `service.py:2675-2680`.

### 1.6 Advisor checkpoint owner

- `AdvisorCheckpointOwner` is at `src/elspeth/web/composer/advisor_checkpoint.py:188-206` (extracted in
  `a11977d79`), constructed at `service.py:651-658`.
- It receives the compose `deadline`, not its own clock:
  `_maybe_run_early_checkpoint(..., deadline=deadline, ...)` (`service.py:2258-2267`).
- Deadline expiry is signalled by the internal `_AdvisorCheckpointComposeDeadlineExpired`
  (`advisor_checkpoint.py:96-102`), which the loop turns into convergence-timeout (`service.py:2268-2271`).
- The per-advisor-call timeout is
  `effective_timeout = configured_timeout if timeout is None else min(configured_timeout, timeout)`, where
  `configured_timeout = settings.composer_advisor_timeout_seconds` (`advisor_checkpoint.py:333-334`,
  `config.py:427` default 60.0). It is applied through `asyncio.wait_for` (`:370-373`).
- The tool-batch ctx carries `advisor_timeout_seconds=self._settings.composer_advisor_timeout_seconds`
  (`service.py:1332`, consumed at `tool_batch.py:2045`) and `deadline=deadline` (`service.py:1345`).
- **Implication:** once `compose()` computes its `deadline` from a remaining budget, the advisor inherits
  it with no further change. Only the planner surface (§1.3) needs separate threading.
- **[grep-2]** The checkpoint compares `deadline` to the loop clock:
  - a checkpoint method takes `deadline: float | None = None` (`advisor_checkpoint.py:912`);
  - per attempt it computes `remaining = deadline - asyncio.get_running_loop().time()` and treats
    `remaining <= 0` as a compose timeout, not as an advisor verdict (`:1016-1025`);
  - a second method takes `deadline: float | None = None` (`:1163`) and forwards it (`:1180`).

### 1.7 Where `llm_calls` are attached to exceptions and persisted

**Attach [grep].** `attach_llm_calls(exc, recorder, *, start_index=0)` is at
`src/elspeth/web/composer/llm_response_parsing.py:752-764`. It sets
`exc.llm_calls = recorder.llm_calls[start_index:]`. Call sites:

| File | Lines |
|---|---|
| `provider_gateway.py` | `:478, 485, 492, 499, 505, 527, 575, 582, 589, 596, 602, 625` |
| `advisor_checkpoint.py` | `:493` |
| `service.py` | `:2434` (deferred cancel), `:2679` (`ChargeableAdmissionRefused`) |
| `tool_batch.py` | `:334` |
| `pipeline_planner.py` | `:2830` |

`ComposerConvergenceError.capture(..., llm_calls=...)` also carries them (`service.py:2637`, `:2654`,
`:2690`).

**Read-back.** `_llm_calls_from_exception(exc)` is at `src/elspeth/web/sessions/routes/_helpers.py:2016-2025`.
It returns `()` when `exc.__dict__["llm_calls_durable"] is True` (`:2018-2019`).

**Route persistence (send).** In `messages.py`:

- the `_BadRequestLLMError` arm (`:678-687`);
- the `ComposerServiceError` arm (`:882-891`);
- the `finally` sweep for untranslated exceptions (`:896-918`);
- the cancelled arm through `_join_shielded_task_after_cancellation(asyncio.create_task(_persist_llm_calls(...)))`
  (`:963-978`).

Recompose mirrors these at `compose.py:478-483`, `:634-639`, `:653-662`, `:705-712` and `:727`.

**Cancel-path constraint.** The comment at `messages.py:576-583` says the compose "MUST stay awaited inline
(no child task): `attach_llm_calls` rides on the CancelledError instance and a task boundary would drop it".

**The writer.** `_persist_llm_calls(service, session_id, llm_calls, composition_state_id, *, plugin_crash_pending: bool, session_operation_context: SessionOperationContext) -> None`
is at `_helpers.py:2028-2099`. It writes one cohort through
`service.add_messages_atomic(session_id, drafts, composition_state_id=..., writer_principal="compose_loop", session_operation_context=...)`
(`:2066-2072`). An unwind failure is counted and logged; a success-path failure is a Tier-1
`AuditIntegrityError`.

**Planner audit writer.** `PlanningApplication._persist_pipeline_planner_audit(self, *, session_id: UUID, current_state_id: UUID | None, llm_calls, planner_attempts, invocations, withheld_replies, session_operation_context: SessionOperationContext | None) -> None`
is at `planning_application.py:299-379`.

- It builds `AuditMessageDraft`s and calls
  `sessions.add_messages_atomic(..., writer_principal="compose_loop", session_operation_context=...)`
  (`:371-377`).
- It raises `TypeError` when the context is `None` (`:368-369`).
- On the failure path (`:792-821`), the planner:
  1. validates that the attached `llm_calls` / `planner_attempts` equal the recorder slice, raising
     Tier-1 otherwise (`:793-803`);
  2. persists them through `_await_pipeline_staging_write_with_deferred_cancellation` (`:804-815`);
  3. sets `exc_dict["llm_calls_durable"] = True` (`:816`).

  That flag is the handshake that stops the route re-persisting (`_helpers.py:2018`), with the comment at
  `messages.py:816-818`.

**`audit_only` need (ruling 2).** `SessionServiceImpl._require_session_operation_context_on_connection(self, conn: Connection, context: SessionOperationContext, *, session_id: str, expected_kind: SessionOperationKind, now: datetime) -> None`
is at `src/elspeth/web/sessions/service.py:970-999`.

- It checks only the exact fence row: `released_at IS NULL AND lease_expires_at > now` (`:987-996`).
- It has **no `audit_only` parameter and no job lookup.**
- **[grep-2]** `grep -rn "audit_only" src/elspeth --include=*.py` finds **0 hits**. No writer in `src/`
  carries such a flag today.
- **[grep-2]** The writer `SessionServiceImpl.add_messages_atomic` is at `sessions/service.py:6893-6902`:

  ```python
  async def add_messages_atomic(self, session_id: UUID, drafts: Sequence[AuditMessageDraft], *,
      writer_principal: ChatMessageWriterPrincipal, composition_state_id: UUID | None = None,
      session_operation_context: SessionOperationContext,
      session_operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE) -> None
  ```

  Every audit-cohort writer listed below funnels through this signature, so it is the natural place to add
  `audit_only`. How its body reaches `_require_session_operation_context_on_connection` was not read.
- Writers that must pass `audit_only=True` under the positive predicate, from the files read:
  - `_persist_llm_calls` (`_helpers.py:2028`);
  - `_persist_turn_audit_cohort` (`_helpers.py:2102`, body not read);
  - `PlanningApplication._persist_pipeline_planner_audit` (`planning_application.py:299`);
  - the compose loop's `_persist_turn_audit` (`service.py:2278`, body not read);
  - the advisor audit persisters imported at `advisor_checkpoint.py:35-41` (`persist_advisor_checkpoint_pass`,
    `persist_advisor_terminal_publication`, bodies not read).
- Every one read goes through `add_messages_atomic`. Its signature and how it reaches
  `_require_session_operation_context_on_connection` were not read.

### 1.8 `ComposerSettings` Protocol

`class ComposerSettings(Protocol)` is at `src/elspeth/web/composer/protocol.py:1498-1609`.

- It declares `composer_timeout_seconds` (`:1548-1549`), `composer_runtime_preflight_timeout_seconds`
  (`:1566-1567`) and `composer_advisor_timeout_seconds` (`:1593-1594`).
- It declares **no** transport ceiling, headroom, sync budget or async-worker properties (Read of the whole
  Protocol body, `:1498-1609`).
- Consumers typed against it: `ComposerServiceImpl` (`service.py:481`), `PlanningApplication`
  (`planning_application.py:227`), `ProviderGateway` (`provider_gateway.py:375`) and
  `AdvisorCheckpointOwner` (`advisor_checkpoint.py:194`).

### 1.9 Every synchronous consumer of `composer_timeout_seconds` now that guided is gone [grep]

**Instrument.** `grep -rn "_timeout_seconds\b\|composer_timeout_seconds" src/elspeth --include=*.py`, with
the frontend filtered out.

- **Positive control:** `config.py:325` is present.
- **Negative control:** the same run returned no hit under `src/elspeth/web/sessions/routes/composer/guided*`
  or `composer/guided/*.py`. `ls src/elspeth/web/composer/guided` shows only `__pycache__` and `skills`.
  The old guided readers (`guided_chat_atomic.py` ×5, `guided.py:5042`, and the service's `:4459`/`:4873`)
  are gone.

The complete `composer_timeout_seconds` reader set is:

| Site | Consumer | Sync after the cutover? |
|---|---|---|
| `config.py:325` | field `composer_timeout_seconds: float = Field(..., gt=0)` | — |
| `config.py:1201-1206` | `_validate_composer_timeout_transport_headroom` | — |
| `config.py:1224,1228` | `_warn_composer_turn_budget_underfunded` | — |
| `app.py:2332` | `/api/system/status` publishes it; the comment at `:2327-2331` says the SPA derives its compose abort ceiling from it | both |
| `service.py:574` → `:885` | `explain_run_diagnostics`, from `POST /api/runs/{run_id}/diagnostics/evaluate` (`execution/routes.py:1389-1478`). **Synchronous.** It holds a **COMPOSE** SOL (`routes.py:1467-1473`) around the LLM call. | **sync** |
| `service.py:574` → `:944` | the `compose()` loop deadline | async (freeform) |
| `planning_application.py:263` → `:737` | the empty-pipeline planner budget | async (freeform) |
| `pipeline_settlement.py:228` | `PipelineCommitConfig(timeout_seconds=request.app.state.settings.composer_timeout_seconds)` inside `settle_pipeline_proposal_under_compose_lock` (`:131-141`). **[grep-2]** It has exactly three callers. **Manual proposal Accept** (`proposals.py:314-320`, under a **PROPOSAL** SOL, `:283-289`) is **sync**. Freeform auto-commit goes through `settle_auto_commit_intent` (`pipeline_settlement.py:405`, delegating at `:429`), called from send (`messages.py:391`) and recompose (`compose.py:220`), which is async. | **mixed** |
| `_helpers.py:3223` | the 422 convergence body `timeout_seconds` for `convergence_wall_clock_timeout` | async |

Non-`composer_timeout_seconds` budget reads that sit in this seam:

- **Tutorial run wait.** `composer/tutorial_service.py:444` sets
  `run_timeout_seconds = settings.composer_transport_idle_ceiling_seconds - settings.composer_transport_headroom_seconds`.
  It is used by `_wait_for_terminal_run` (`:505-521`, poll `_TUTORIAL_RUN_POLL_SECONDS = 0.25` at `:70`,
  504 `tutorial_run_timeout` at `:516-520`). This is **sync** and reads ceiling − headroom, not the compose
  budget.
- **Tutorial Build.** The server tutorial module imports no composer service (import block
  `tutorial_service.py:1-66`, Read). After `01d96af9a` ("Move first-run tutorial Build to freeform
  Composer") the Build reaches the composer through the SPA's ordinary freeform send. **[grep-2]** The
  call is `void composer.sendMessage(tutorialBrief(sampleUrls));` at
  `src/elspeth/web/frontend/src/components/tutorial/TutorialFreeformShell.tsx:112`. Under the cutover the
  tutorial Build becomes async with no code of its own, which is exactly what composer invariant 2
  requires.
- **Auto-title.** `messages.py:1037` has `asyncio.wait({auto_title_task}, timeout=2.0)`, a fixed 2 s, and
  does not read the compose budget.
- **Test driver.** `service.py:771` is `_run_one_turn_for_test`.

### 1.10 Config: `WebSettings` and the headroom coupling

| Item | Anchor | Content |
|---|---|---|
| "Derive it, do not type it" comment | `config.py:53-73` | Cloudflare 125 s incident (elspeth-ad5628ecda) |
| defaults | `config.py:74-75` | `_DEFAULT_COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS = 300.0`, `_DEFAULT_COMPOSER_TRANSPORT_HEADROOM_SECONDS = 30.0` |
| planning floor | `config.py:81` | `_COMPOSER_PLANNING_SECONDS_PER_TURN = 15.0` |
| `composer_timeout_seconds` | `config.py:325` | `Field(..., gt=0)`, required |
| `composer_transport_idle_ceiling_seconds` | `config.py:335-348` | with its description |
| `composer_transport_headroom_seconds` | `config.py:349-352` | |
| `composer_runtime_preflight_timeout_seconds` | `config.py:353` | |
| `composer_advisor_timeout_seconds` | `config.py:427` | |

`_validate_composer_timeout_transport_headroom` is at `config.py:1186-1208`, unchanged in content from the
old findings:

- `:1198-1200` is the internal-consistency check (`ceiling - headroom <= 0` → `ValueError`). Keep it.
- `:1201-1207` is the compose-budget coupling (`composer_timeout_seconds > ceiling - headroom` →
  `ValueError`). Spec §3 removes it.

`_warn_composer_turn_budget_underfunded` is at `config.py:1210-1233`.

### 1.11 Deployment mirrors and the tests that pin them (re-read at the pinned tip)

**Mirrors:**

- **ECS variables.** `deploy/aws-ecs/terraform/modules/scenario/variables.tf`:
  - `alb_idle_timeout_seconds` at `:611-620` (default 900, validation [60, 4000]);
  - `composer_timeout_seconds` at `:622-638` (default 840, comment `:627-633`);
  - the plan-time cap `var.composer_timeout_seconds <= var.alb_idle_timeout_seconds - 30` at `:635-636`.
- **ECS locals.** `locals.tf:472-482`: the "coupled three-leg chain" comment at `:472-480`, the ceiling env
  wired to the ALB variable at `:481`, and the timeout env at `:482`.
- **ACA jq.** `deploy/azure-container-apps/scripts/validate-workload-parameters.jq`:
  - `:51` requires `composerTransportIdleCeilingSeconds | positive_integer and . <= 240`;
  - **`:54` requires `composerTimeoutSeconds <= (composerTransportIdleCeilingSeconds - 30)`**;
  - `:19-20` and `:25` reserve the ceiling, headroom and timeout env names.
- **ACA bicep.** `deploy/azure-container-apps/workload.bicep:80-83` is the ceiling parameter
  (`@maxValue(240)`, "ingress request timeout is a fixed 240 seconds"). `:93-95` is the
  `composerTimeoutSeconds` description, "must leave the runtime-required headroom below the transport
  ceiling".
- **Compose.** `deploy/compose/web-postgres.yaml:16-19` sets 300 / 360 / 30, with the comment "Matched to
  nginx.conf". `deploy/compose/nginx.conf:33-34` sets `proxy_read_timeout 360s; proxy_send_timeout 360s;`.
- **systemd.** `deploy/linux-systemd/elspeth-web.env.example:20,22,23` sets 180 / 240 / 30.

**Pinning tests:**

- `tests/unit/web/test_config.py`:
  - `:232-240` `test_composer_timeout_zero_rejected` (keep);
  - `:242-250` `test_composer_timeout_must_leave_transport_headroom` (must invert);
  - `:252-263` `test_composer_timeout_allows_explicit_larger_transport_ceiling`.
- `tests/unit/deployment/test_aws_ecs_terraform_package.py:3369-3471`
  `test_composer_wall_clock_fits_under_the_app_guard_and_the_alb`:
  - no headroom override (`:3397-3401`);
  - headroom from the `WebSettings` default (`:3402`);
  - ceiling and timeout env wiring (`:3404-3416`);
  - the plan-time cap regex, which must equal the headroom (`:3435-3449`);
  - `idle_timeout` wiring (`:3451-3457`);
  - **`timeout + headroom <= alb_idle` (`:3459-3463`)**;
  - floor `>= 840` (`:3468-3471`).
- `tests/unit/deployment/test_nginx_websocket_template.py:47-73`: nginx timeouts equal the ceiling
  (`:60-62`), and **`timeout <= ceiling - headroom` (`:63-65`)**.
- `tests/unit/deployment/test_azure_container_apps_launch_inputs.py:207-235`: the mutated-parameter cases
  `("composerTransportIdleCeilingSeconds", 241)` (`:213`), `("composerTimeoutSeconds", 181)` (`:214`, which
  the jq at `:54` rejects), and a headroom override (`:215`).

**Re-anchor owed** (not re-read at this tip; the old anchors are from `findings/app-lifespan-config.md` §5-6):

- ECS `README.md` §"Composer wall-clock budget" (old `:993-1040`);
- ACA `README.md:137-152`;
- `docs/runbooks/azure-container-apps-deployment.md` and `-cold-install.md` (old `:399`);
- `docs/guides/docker.md:64-66,80,350-356`;
- `docs/reference/environment-variables.md:161`;
- `tests/unit/web/test_app.py:760-780` (system status);
- `tests/unit/web/composer/test_tutorial_service.py:688-693`;
- `tests/unit/deployment/test_compose_bundle.py:289-298`;
- `tests/unit/deployment/test_linux_systemd_bundle.py:111-119`;
- `tests/unit/docs/test_docker_guide_release_examples.py:68`;
- `tests/unit/web/test_azure_container_apps_runbook_contract.py:248-258`;
- `tests/unit/deployment/test_azure_container_apps_bundle.py:534,587,828`;
- `tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py:75`.

### 1.12 Request-bound seams the worker cannot call as they are

| Symbol | Anchor | Why |
|---|---|---|
| `_SessionComposeLockRegistry` and `_get_session_compose_lock_registry(request: Request)` | `_helpers.py:217`, `:258` | Takes a `Request`. Its callers include `messages.py:162`, `compose.py:117`, `proposals.py:280` and `execution/routes.py:1456`. |
| `_composer_progress_sink(registry, request=..., ...)` | `_helpers.py:288` | Called at `messages.py:262-268` and `compose.py:172-178`. |
| `_cancel_on_client_disconnect(request)` | `_helpers.py:2324` | Mounted at `messages.py:584`, `compose.py:395`. |
| `_track_compose_inflight(session_id, request, user)` | `_helpers.py:2556-2595` | Its docstring says it is "wired into `send_message` and `/recompose`". Heartbeat constants: `_COMPOSER_HEARTBEAT_SECONDS = 15.0` (`:2452`), `_COMPOSER_REQUEST_LEASE_SECONDS = 60` (`:2454`). **[grep-2]** It has exactly two mounts in `src/`: `messages.py:142` and `compose.py:102`. |
| `settle_pipeline_proposal_under_compose_lock(*, request: Request, user: UserIdentity, ...)` | `pipeline_settlement.py:131-141` | Still takes `request` and `user`, so D6 is still owed. |

---

## 2. Delta against the old findings and contract (measured at `d479eb2b4`)

### 2.1 What moved

| Symbol | Old anchor | Anchor at `1effedab2` |
|---|---|---|
| `compose()` | `service.py:4112-4126` | `service.py:898-912` |
| compose deadline | `service.py:4163` | `service.py:944` |
| `self._timeout_seconds =` | `service.py:2542` | `service.py:574` |
| `explain_run_diagnostics` timeout | `service.py:4099` | `service.py:885` |
| `_run_one_turn_for_test` deadline | `service.py:2719` | `service.py:771` |
| empty-pipeline planner | `ComposerServiceImpl._plan_and_stage_empty_pipeline` `service.py:5289` / budget `:5382` | **`PlanningApplication._plan_and_stage_empty_pipeline` `planning_application.py:656`**, budget `:263` → `:737` |
| `PlannerModelConfig.timeout_seconds` | `pipeline_planner.py:684`, consumed `:3775` | `:616`, consumed `:3093` |
| `ComposerSettings` Protocol | `protocol.py:1506`, timeout `:1557` | `protocol.py:1498`, timeout `:1549` |
| status publication | `app.py:2334` | `app.py:2332` |
| settlement timeout read | `pipeline_settlement.py:273`, function `:173` | `:228`, function `:131` |
| proposal Accept caller | `proposals.py:318` | `proposals.py:314-320` |
| convergence 422 `timeout_seconds` | `_helpers.py:3143` | `_helpers.py:3223` |
| `_track_compose_inflight` | `_helpers.py:2442` | `_helpers.py:2556` |
| heartbeat constants | `_helpers.py:2338/2340` | `_helpers.py:2452/2454` |
| compose-lock registry / accessor | `_helpers.py:311-349 / 352-364` | `_helpers.py:217 / 258` |
| tutorial run wait | `tutorial_service.py:413`, `_wait_for_terminal_run` `:474-490` | `:444`, `:505-521` |
| ECS wall-clock test | `test_aws_ecs_terraform_package.py:3365-3468` | `:3369-3471` |
| `send_message` | module-level route | nested in `register_message_routes(router)` (`messages.py:128-143`) |

Unchanged:

- `config.py:74-75, 81, 325, 335-353, 427, 1186-1233`;
- `execution/routes.py:1389-1393, 1474`;
- every deploy mirror line in §1.11;
- `test_config.py:232-263`, `test_nginx_websocket_template.py:47-73` and
  `test_azure_container_apps_launch_inputs.py:207-235`.

### 2.2 What vanished

- **Every guided consumer of the budget:**
  - `guided_chat_atomic.py:729,790,836,874,897`;
  - `guided.py:5042`;
  - `plan_guided_full_pipeline` (old `service.py:4381/4459`);
  - `plan_guided_pipeline` (old `:4543/4873`);
  - the guided route mounts of `_track_compose_inflight` (`guided_plan.py:338`, `guided.py:2941,5872`).

  [grep]: zero `composer_timeout_seconds` / `_timeout_seconds` readers in any guided path. The
  `composer/guided/` directory holds only `skills/`.
- **The spec §3 / contract D11 "three guided routes"** and "guided planners" consumer rows. Their
  remaining sync consumers are `explain_run_diagnostics` and proposal Accept.

### 2.3 What is new

1. **Two budget clocks per request** (§1.3). The planner's budget lives on `PlanningApplication`, not on
   `compose()`. Contract T08's "`_plan_and_stage_empty_pipeline(..., budget_seconds)` threads it to
   `PlannerModelConfig.timeout_seconds`" must now name `PlanningApplication` (`planning_application.py:656`,
   `:737`).
2. **`ProviderGateway`** (`provider_gateway.py:364`) takes the per-call `timeout` from its caller and sends
   no LiteLLM timeout (§1.4). The **gateway sidecar** has an independent 300 s per-call cap (`a375d7f13`)
   that no deployment mirror sets.
3. **`ComposerChargeableAdmission.require`** runs before the deadline is computed (`service.py:942` vs
   `:944`). It is the source of `ComposerAdmissionRefused` → 403.
4. **`AdvisorCheckpointOwner`** inherits the compose `deadline` (`service.py:2258-2267`). Its per-call
   timeout is `min(composer_advisor_timeout_seconds, caller timeout)` (`advisor_checkpoint.py:333-334`).
5. **Message ingress receipts** (`8630db9b8`), which ruling 1 must delete:
   - `_ingress_receipt_conflict` at `messages.py:111-125`, with the 409 `message_already_accepted` /
     `message_idempotency_conflict` bodies;
   - the pre-check `service.lookup_message_ingress(session.id, client_request_id=body.client_request_id, content=..., requested_state_id=body.state_id, session_operation_context=...)`
     at `messages.py:173-181`;
   - the ingress write `service.add_message_with_transcript(session.id, "user", body.content, client_request_id=body.client_request_id, requested_state_id=body.state_id, composition_state_id=pre_send_state_id, writer_principal="route_user_message", session_operation_context=...)`
     inside `compose_operation_lease.create_task(...)` (`messages.py:236-256`).
6. **`RecomposeRequest.expected_user_message_id`** already exists. It is checked at `compose.py:158-165`
   (409 `recompose_user_message_mismatch`), and recompose composes against the current head
   (`compose.py:129-135`). Recompose has **no `state_id`**, so ruling 5's recompose `state_id` is a new
   field.
7. **The tutorial Build rides freeform `/messages`** (`01d96af9a`, §1.9). The tutorial run wait still
   uses ceiling − headroom.
8. **`explain_run_diagnostics` holds a COMPOSE SOL** for the duration of its LLM call
   (`execution/routes.py:1467-1478`). The old findings named it only as a timeout consumer.
9. **The planner's `llm_calls_durable` handshake** (`planning_application.py:816` ↔ `_helpers.py:2018`) and
   the rule that compose stays inline (`messages.py:576-583`) are constraints on the worker's task
   structure.

### 2.4 Contract rows affected

- **T08** (`contract.md:318-323`) is stale:
  - "Guided planners … read `composer_sync_timeout_seconds`": the guided planners are gone.
  - The planner threading target is now `PlanningApplication`.
- **D11** (`contract.md:81`) is partly stale. The "three guided routes, the guided planners" consumers are
  gone. `explain_run_diagnostics`, proposal-decision settlement and `/api/system/status` remain.
- **Settings block** (`contract.md:88-102`): `ComposerSettings` is at `protocol.py:1498`, not `:1506`.
- **F-C5** (`contract.md:51`): the remaining budget must reach **both** `compose()`'s deadline (`:944`)
  **and** `PlannerModelConfig.timeout_seconds` (`planning_application.py:737`). F-C5's "≤ 0 settles
  `deadline_expired` before any provider call" is also a hard precondition, because
  `PlannerModelConfig.__post_init__` raises on a non-positive value (`pipeline_planner.py:711-714`).

---

## 3. Implications for the plan under the rulings

1. **Budget threading (T08 / F-C5).**
   - Add one keyword to `compose()` (Protocol `protocol.py:1622` and impl `service.py:898`), either
     `budget_seconds: float | None = None` or an absolute monotonic deadline.
   - Use it at `:944`, and pass the value **remaining at the branch** into
     `PlanningApplication._plan_and_stage_empty_pipeline` (a new keyword) → `PlannerModelConfig(timeout_seconds=...)`.
   - Otherwise the planner surface silently runs a full `composer_timeout_seconds` after the job's deadline
     has partly elapsed.
   - `PlanningApplication._timeout_seconds` (`:263`) then becomes a default only, or is removed.
   - The advisor and tool batch need nothing more (§1.6).
2. **The admission call precedes the clock** (`service.py:942`). Under the worker, the job's `deadline_at`
   already covers it, because the worker measures remaining before calling `compose()`. Only the
   in-`compose()` deadline excludes it. D10's reported `timeout_seconds` should say which budget it
   reports.
3. **Gateway sidecar.**
   - The async cutover lets a compose budget exceed 300 s everywhere, including ACA, where today's cap is
     210 s.
   - When the sidecar is in path, a single provider call longer than 300 s is then cut by the sidecar
     rather than by `remaining`.
   - The plan should decide one of three things: document the relationship; add an ECS/ACA mirror for
     `ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS`; or leave the 300 s per-call bound as intended.
   - It should also measure how `UPSTREAM_TIMEOUT` classifies (§1.4) before D10's convergence body is
     trusted to report the right budget.
   - This is a relationship check, not a request to raise either timeout.
4. **D11 shrinks.** With guided gone, `composer_sync_timeout_seconds = min(timeout, ceiling − headroom)`
   has three consumers:
   - `explain_run_diagnostics` (`service.py:885`), which reads the budget through the `ComposerSettings`
     Protocol and needs the new Protocol property at `protocol.py:1498`;
   - proposal Accept settlement (`pipeline_settlement.py:228` via `proposals.py:314`);
   - `/api/system/status` (`app.py:2332`).

   `settle_pipeline_proposal_under_compose_lock` serves both the sync Accept and the async auto-commit
   from **one** settings read, so it needs a caller-supplied `timeout_seconds` (or D6's signature change
   carries it). Otherwise either the async turn is capped at the sync bound, or Accept inherits an
   unbounded budget.
5. **Config decoupling (T15).**
   - Keep `config.py:1198-1200`; remove `:1201-1207`.
   - Invert `test_config.py:242-250`.
   - Change the ECS cap (`variables.tf:634-637`), the ECS test (`:3435-3449`, `:3459-3463`), the nginx
     test `:63-65`, the ACA jq `:54`, the bicep description `:93`, and the ACA case `:214`.
   - Keep the ceiling-bound checks, which still protect the tutorial run wait (`tutorial_service.py:444`)
     and the sync consumers in item 4.
   - The ECS floor `>= 840` (`test_aws_ecs_terraform_package.py:3468-3471`) is unaffected.
6. **Ruling 2 (positive fence predicate).**
   - The predicate goes into `_require_session_operation_context_on_connection` (`service.py:970-999`),
     which has no `audit_only` flag today. Every audit-cohort writer in §1.7 must thread `audit_only=True`
     down through `add_messages_atomic`.
   - T06's post-terminal write inventory must include:
     - `_persist_llm_calls`;
     - `_persist_turn_audit_cohort`;
     - `_persist_pipeline_planner_audit`;
     - the compose loop's `_persist_turn_audit`;
     - the advisor audit persisters;
     - the route arms that call them, including the `finally` sweep at `messages.py:896-918` and the
       cancelled arm at `:963-978`.
   - The `llm_calls_durable` flag must still be honoured once these bodies move into `run_composer_turn`.
7. **Worker task structure.** `compose()` must be awaited inline in the turn task, not as a child task,
   or the cancel-path `llm_calls` are lost (`messages.py:576-583`). T10 and T11 should state this as a
   constraint.
8. **Ruling 1 (one id).** Deletion targets at this tip:
   - `messages.py:111-125`, `:173-181`, and the conflict branch at `:253-254`;
   - the 409 bodies' `client_request_id` field.

   The worker writes ingress through `add_message_with_transcript` (`messages.py:238-247`) under its bound
   SOL. The composite FK design is in `RECOMMENDATION.md` T03.
9. **Ruling 5 (refuse a moved base).**
   - Send already carries `body.state_id` (`messages.py:192-204`), and today it is used **only** as
     provenance (`pre_send_state_id`). The comment at `:206-218` records why the loop seeds from the head
     rather than the client id (elspeth-e08063c3a5).
   - Ruling 5 reverses that for the *queued* window only: a mismatch between the bound base and the head
     at start is a terminal 409 `stale_compose_state`, before the user row.
   - The plan must keep the in-turn seeding from the head, so that a legitimately lagging client is not
     turned into a permanent 409. Otherwise elspeth-e08063c3a5 returns.
   - Recompose needs a new `state_id` (§2.3 item 6).
10. **COMPOSE SOL contention from diagnostics.** `evaluate_run_diagnostics` takes a COMPOSE SOL for the
    whole LLM call (`execution/routes.py:1467-1478`, bounded by `composer_timeout_seconds` today).
    - A queued job's start composite meets `SessionOperationConflictError` and releases its claim
      (contract "Per job").
    - The F-M2 discovery skip hides that session while diagnostics runs.
    - Starvation tests (T16) should include diagnostics as well as fork/revert.
    - Conversely, `evaluate_run_diagnostics` has no visible arm for `SessionOperationConflictError`
      (`routes.py:1479-1507` read). What it returns while a job is running is **NOT MEASURED**.
11. **Tutorial parity (invariant 2).** The tutorial Build now uses freeform `/messages`, so the cutover
    carries it automatically. The plan must not add any tutorial-specific polling or synchronous
    fallback. The tutorial *run* wait stays synchronous and ceiling-bound.
12. **Worker parity with the HTTP envelope.** `PlanningApplication._planner_request_lifecycle`
    (`planning_application.py:272-297`) assumes that rate limiting and in-flight/disconnect scopes already
    wrap `compose()`. The worker supplies the equivalent through D1's CRL and the job cancellation path.
    The docstring needs updating when it moves.

---

## 4. Open questions

1. **Does `composer_sync_timeout_seconds` still earn its place** now that guided is gone? The alternative
   is that `explain_run_diagnostics` and proposal Accept each take an explicit bound from their route.
   Either way, `ComposerSettings` (`protocol.py:1498`) needs a property if the service computes it.
2. **Which budget does freeform auto-commit settlement get inside the worker:** the remaining job budget,
   or a fixed commit budget? The current single read is `pipeline_settlement.py:228`, and
   `settle_auto_commit_intent` is called at `messages.py:391` and `compose.py:220`.
3. **Is the gateway sidecar in any shipped composer path?** How does its `UPSTREAM_TIMEOUT` classify
   through LiteLLM and `classify_provider_failure`? Should composer budgets above the sidecar timeout be
   refused, documented, or left alone?
4. **Planner budget shape:** pass the remaining seconds at the branch, or pass an absolute deadline so that
   `plan_pipeline` stops starting its own clock at `pipeline_planner.py:3093`?
5. **What does `evaluate_run_diagnostics` return when a compose job holds the COMPOSE SOL**, and does the
   plan accept that behaviour for jobs lasting minutes?
6. **Still owed a read:**
   - the bodies of `add_messages_atomic` (`service.py:6893`), `_persist_turn_audit_cohort`,
     `_persist_turn_audit` and the `advisor_audit.py` persisters, which bound the T06 inventory for
     ruling 2;
   - the "re-anchor owed" docs and tests in §1.11.
