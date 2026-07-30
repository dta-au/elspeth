# Phase 3: ComposerCompletionClient Boundary + Caller Migration

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Route every runtime Composer LLM call through one injected boundary with two implementations (direct = today's LiteLLM behaviour, gateway = the Phase 1 contract), per `docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-integration-design.md` "Composer integration".

**Why it is harder than the design implies:** the Composer surface has three *different* response-consumption contracts, LiteLLM exception types are caught at route level (not just at call sites), and the files involved carry judge-signed AST fingerprints. All three are addressed below.

## Ground truth (surveyed 2026-07-30, HEAD 8644fc4c3)

**The helper:** `_litellm_acompletion` is defined at `src/elspeth/web/composer/service.py:498-508` — lazy-imports litellm, applies OpenRouter attribution headers via `_apply_openrouter_app_identity` (453-495), no retry/timeout/telemetry inside. Every wrapping is per-caller, which is why 6+ near-duplicate try/except/finally blocks exist.

**Runtime call sites (all must migrate):**
| Site | File | Notes |
|---|---|---|
| `_call_llm` / `_call_text_llm` | service.py:4535 / 4569 | tools; temperature+seed conditional; no tool_choice/response_format |
| `_call_advisor_with_audit` | service.py:5023 | advisor model, `max_tokens`, `asyncio.wait_for` at 5093 |
| `_call_llm_with_audit` / `_call_text_llm_with_audit` | service.py:5448 / 5552 | Anthropic cache markers applied first |
| `_call_llm_before_deadline` | service.py:5653 | the retry/deadline owner: `_LLM_API_MAX_ATTEMPTS=3`, base delay 1.0s |
| planner completion | pipeline_planner.py:1640 via `PlannerModelConfig.completion` | 3 constructions: service.py:2400, 2674, 2962 |
| 4 guided step solvers | guided/chat_solver.py:1170, 1588, 2070, 2304 via `_bounded_acompletion` (1462) | primary model only |
| boot probe | composer/boot_probe.py:48 | called from app.py:585-638 for BOTH primary and advisor, 5s each |
| auto-title | sessions/_auto_title.py:121 | primary only, best-effort, no audit row |

**Acceptance-only (amendment 2 disposition):** `src/elspeth/web/_aws_ecs_acceptance/bedrock.py:42` already takes `completion: Callable[..., Awaitable[Any]] = _litellm_acompletion` as an injectable parameter. It is the AWS Bedrock acceptance lane, not a runtime Composer path.

## Decisions taken (do not relitigate)

1. **Amendment 2 disposition — `_aws_ecs_acceptance/bedrock.py` is EXEMPT from the boundary migration, and the exemption is recorded in code.** It is an acceptance lane that deliberately exercises *Bedrock specifically*; routing it through a provider-agnostic boundary would destroy what it tests. It is already DI-shaped. Add a module docstring line stating the exemption and referencing acceptance criterion 7, plus a test asserting it is the ONLY `_litellm_acompletion` reference outside the boundary module (so the exemption cannot silently grow).
2. **Amendment 3 binds here.** `WebSettings.composer_seed` (web/config.py:167) is the trigger. When a role resolves to a gateway profile AND `composer_seed is not None`, `seed` must be in that profile's `required_capabilities` — enforced at config validation, so a seedless adapter fails at startup rather than per-turn. Seed sites to honour: app.py:596,606; service.py:2404,2674,2966,4551,4583,5091; guided_chat_atomic.py:231,250,275,293; messages.py:274.
3. **Response shape: the client returns a litellm-compatible object, not a dict.** The freeform loop (`service.py`/`tool_batch.py:448-465`) uses hard attribute access (`response.choices[0].message.tool_calls[i].function.name`); `pipeline_planner._provider_field` (546-566) tolerates either; `bedrock._bedrock_content` has a third accessor. The gateway client therefore returns frozen dataclasses mirroring the litellm shape (the same shape `tests/unit/web/composer/_helpers.py:119-237` already models as `FakeLLMResponse`/`FakeChoice`/`FakeMessage`/`FakeToolCall`/`FakeFunction`). Rewriting the freeform loop is explicitly out of scope.
4. **Exceptions: ELSPETH-owned types, and this is a PARITY SWEEP, not a call-site change.** LiteLLM exception classes are caught at route level too — `sessions/routes/messages.py:280-281,340`, `sessions/routes/composer/compose.py:166-167`, `sessions/_guided_step_chat.py:61-64`. A gateway error that is not a real `litellm.exceptions.*` instance would fall through those `except` blocks **uncaught**. So: define ELSPETH-owned Composer LLM error types, have the DIRECT client translate litellm exceptions into them, and update EVERY `except` site — call-site and route-level. Before writing code, `grep -rn "litellm.exceptions\|LiteLLMAPIError\|LiteLLMAuthError\|LiteLLMBadRequestError" src/elspeth/web/` and enumerate every discriminator; a guard that still names a litellm type passes vacuously for gateway errors.
5. **Retry ownership is unchanged.** `pipeline_planner.py:1631-1632` pins `num_retries: 0, max_retries: 0` so LiteLLM never retries internally — the planner is the sole retry owner ("one physical attempt = one audited ordinal"). The gateway's single OAuth-refresh replay is a transport-level retry *inside one attempt* and must NOT create a second audited ordinal. Assert this.

## Global Constraints

- Every runtime Composer LLM path resolves through the boundary. `grep -rn "_litellm_acompletion" src/elspeth/web/` must, at phase end, return only: the boundary module's direct implementation, and the documented `_aws_ecs_acceptance/bedrock.py` exemption. A test enforces this.
- `composer_profile` / `composer_advisor_profile` are new OPTIONAL operator settings. When present, a profile selector takes authority for that role; otherwise the existing `composer_model` / `composer_advisor_model` remain authoritative. Mixed direct/gateway roles are permitted.
- **Preserve the two-model independence rule.** `WebSettings._validate_advisor_distinct_from_primary` (web/config.py:808-828) canonicalises `model_id.rsplit("/", 1)[-1].strip()` and rejects a match. Profile indirection must re-derive canonical model identities BEFORE that check, or a primary profile and an advisor model pointing at the same underlying model would slip through.
- **Preserve the advisor asymmetry:** guided step solvers and auto-title use the primary model only; the advisor is used solely by `_call_advisor_with_audit` and the planner escape hatch.
- Audit unchanged in shape: `build_llm_call_record` (`composer/llm_response_parsing.py:479-529`) inputs must still be supplied, and `_safe_llm_error_message`'s secret-redaction (67-80, 462-476) must cover any new gateway error text.
- Cancellation/timeout: the boundary must raise `asyncio.CancelledError`/`TimeoutError` promptly under the EXISTING `asyncio.wait_for` wrappers. Do NOT add a second timeout layer.
- Tutorial paths (`PlannerSurface.TUTORIAL_PROFILE`, the guided step solvers) are release canaries — a regression there is canary-severity, never routine test noise.
- Commit by pathspec; ruff + mypy before every commit; no venv mutation.

## Expected, non-blocking: judge-signature drift

`config/cicd/enforce_tier_model/web.yaml` carries judge-signed suppressions keyed to exact AST fingerprints for `web/composer/service.py` (lines 728, 747, 766, 785, 804, 823, 2201, 2220, 3519, 3538), `web/composer/guided/chat_solver.py` (2963, 2982, 3001, 3020), `web/sessions/_auto_title.py:325`, `web/composer/boot_probe.py:405`. Adding an import to any of these files rotates their fingerprints. **This is expected drift that only the operator can clear** — signing requires `ELSPETH_JUDGE_METADATA_HMAC_KEY`, which an agent must never hold ([O1] custody rule, elspeth-b3a3335c9f), and signing never runs in CI. Per standing posture, signed-entry drift is reconciled ONCE at the release-branch merge. **Do not hand-edit any `judge_metadata_signature`; do not attempt a judge run.** Record the drift and surface it at phase close.

---

### Task 1: The boundary protocol and the direct implementation

**Files:** create `src/elspeth/web/composer/completion_client.py`; tests `tests/unit/web/composer/test_completion_client.py`.

**Produces:**
- `ComposerCompletionRequest` (frozen): `model`, `messages`, `tools`, `tool_choice`, `response_format`, `temperature`, `seed`, `max_tokens`, `extra_headers`.
- Response shim dataclasses mirroring the litellm shape (`ComposerFunction`, `ComposerToolCall`, `ComposerMessage`, `ComposerChoice`, `ComposerCompletionResponse`) with the SAME attribute names the freeform loop reads.
- ELSPETH-owned errors: `ComposerLLMAuthError`, `ComposerLLMBadRequestError` (carrying `provider_detail`/`provider_status_code` as `_BadRequestLLMError` does today), `ComposerLLMAPIError`, `ComposerLLMMalformedResponseError`.
- `class ComposerCompletionClient(Protocol)`: `async def complete(self, request: ComposerCompletionRequest) -> ComposerCompletionResponse`.
- `DirectCompletionClient` — wraps today's `_litellm_acompletion` verbatim (including `_apply_openrouter_app_identity`) and TRANSLATES litellm exceptions into the ELSPETH-owned types.

- [ ] Tests first: the direct client reproduces today's kwargs byte-for-byte for a representative call; each litellm exception class maps to its ELSPETH counterpart preserving `provider_detail`/`provider_status_code`; the response shim satisfies attribute access exactly as `_helpers.FakeLLMResponse` does.
- [ ] Commit: `feat(composer): add the completion-client boundary and direct implementation`

### Task 2: Exception parity sweep

**Files:** service.py, pipeline_planner.py, guided/chat_solver.py, boot_probe.py, sessions/_auto_title.py, sessions/routes/messages.py, sessions/routes/composer/compose.py, sessions/_guided_step_chat.py.

- [ ] **Step 1:** run the enumeration grep from Decision 4 and write the complete discriminator list into the task report BEFORE editing.
- [ ] **Step 2:** replace every `except litellm.exceptions.*` with the ELSPETH-owned equivalent, including the three route-level sites. Delete the now-unused litellm imports.
- [ ] **Step 3:** a test asserting `grep`-equivalent absence: no `litellm.exceptions` reference remains under `src/elspeth/web/` outside the boundary module and the bedrock acceptance lane.
- [ ] Commit: `refactor(composer)!: raise ELSPETH-owned LLM errors instead of litellm types`

### Task 3: Migrate the service call sites

- [ ] Inject the client into `ComposerServiceImpl`; migrate `_call_llm`, `_call_text_llm`, `_call_advisor_with_audit`, `_call_llm_with_audit`, `_call_text_llm_with_audit`, `_call_llm_before_deadline`. Preserve the Anthropic cache markers, the deadline/retry semantics, and every `build_llm_call_record` input.
- [ ] Commit: `refactor(composer): route service LLM calls through the client boundary`

### Task 4: Migrate planner, guided solvers, boot probe, auto-title

- [ ] `PlannerModelConfig.completion` becomes the client (keep `_provider_field`'s tolerance — the shim satisfies both). Migrate the four `chat_solver` solvers via `_bounded_acompletion`, the boot probe, and auto-title. Update the ~20 per-module monkeypatch sites to inject a fake client instead.
- [ ] Commit: `refactor(composer): route planner, guided, boot-probe and auto-title through the boundary`

### Task 5: The gateway implementation

- [ ] `GatewayCompletionClient` speaking contract major 1: same strict envelope handling, error-code mapping, and non-leak discipline as the Phase 2 provider (reuse the mapping table; do not re-derive it). Returns the response shim. Supports message history, tool definitions, tool_choice, assistant tool calls, tool results, text completions, timeout/cancellation, and returned model identity.
- [ ] The OAuth-refresh replay must not create a second audited ordinal (Decision 5) — assert one audited call per logical attempt.
- [ ] Commit: `feat(composer): add the gateway completion client`

### Task 6: Profile-based role resolution + seed capability (amendment 3)

- [ ] `composer_profile` / `composer_advisor_profile` optional settings; per-role selection; server-scope requirement; the independence rule re-derived over resolved model identities (Global Constraints).
- [ ] **Amendment 3:** `composer_seed is not None` AND the role resolves to a gateway profile ⇒ `seed` must be in that profile's `required_capabilities`, enforced at config validation with a clear message. Test both directions.
- [ ] Boot probe: a gateway binding verifies readiness AND performs a real minimal completion; a direct binding keeps today's probe. Both roles probed.
- [ ] Commit: `feat(composer): profile-based role resolution with seed capability enforcement`

### Task 7: Phase close

- [ ] The no-un-migrated-caller test passes (only the boundary + the documented bedrock exemption).
- [ ] Full `pytest tests/` green; `elspeth-lints check` — expect judge-signature drift on the four files above; record it, do NOT sign.
- [ ] End-to-end: a Composer tool-call/tool-result round trip against the Phase 1 reference gateway (design acceptance criterion 8).
- [ ] Write the Phase 4 plan (Terraform Scenario C).
