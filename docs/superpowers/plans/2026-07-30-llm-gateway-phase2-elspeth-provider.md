# Phase 2: Shared LLM Profile Contract + Pipeline Gateway Provider

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `gateway` a first-class pipeline LLM provider, and give web and batch/CLI one shared operator-owned LLM profile model — per `docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-integration-design.md` ("Shared LLM profile contract", "Pipeline provider integration").

**Architecture:** The provider-neutral profile models move out of `src/elspeth/web/plugin_policy/profiles.py` into a new `src/elspeth/core/llm_profiles.py` (flag-day, no shims). A new `GatewayConfig` + `GatewayLLMProvider` join the existing `_PROVIDERS` registry, implementing the current three-method `LLMProvider` protocol over HTTP against the Phase 1 gateway contract major 1.

**Tech Stack:** pydantic v2, httpx via the existing `AuditedHTTPClient`, the existing typed LLM error hierarchy.

## Ground truth this plan is built on (verified 2026-07-30, HEAD e90e838eb)

- `_PROVIDERS` registry: `src/elspeth/plugins/transforms/llm/transform.py:253`. Protocol: `provider.py:115`. `LLMQueryResult`: `provider.py:82` (`content`, `usage: TokenUsage`, `model`, `finish_reason`), rejects blank content/model in `__post_init__`.
- Provider Literal to extend: `base.py:67` (+ docstring line 49).
- OpenRouter (`providers/openrouter.py`) is the HTTP template — it records **two** audit rows: the transport row from `AuditedHTTPClient` plus a semantic `CallType.LLM` row via `_record_logical_llm_success` / `_record_logical_llm_error` (openrouter.py:454-506). `resolved_prompt_template_hash` attaches only to the LLM row. **The gateway provider must follow this two-row pattern.**
- `response_format` is constructed ONLY in `MultiQueryStrategy._execute_one_query` (`transform.py:611-628`); `SingleQueryStrategy` never passes it. Shapes: `{"type":"json_object"}` and `{"type":"json_schema","json_schema":{"name":...,"schema":{...}}}` — these match the Phase 1 gateway contract exactly.
- Usage: `TokenUsage` is all-optional; unavailable usage is `TokenUsage.unknown()`, never zeros (`clients/llm.py:203`). Finish reason: absent → `None`; unrecognized → `UnrecognizedFinishReason`; **never** call `parse_finish_reason(None)`.
- Typed errors live in `elspeth.plugins.infrastructure.clients.llm`: `RateLimitError NetworkError ServerError ContentPolicyError ContextLengthError LLMClientError`.
- Layering is one-way: `src/elspeth/core/` NEVER imports `src/elspeth/plugins/`; `plugins/` imports `core/`. This fixes the profile-model destination (below).

## Decisions taken (do not relitigate)

1. **Destination = `src/elspeth/core/llm_profiles.py`, NOT `plugins/transforms/llm/`.** `ElspethSettings` (`core/config.py:1801`) must type the new top-level catalog, and core cannot import plugins. The provider-allowlist validator keeps the existing lazy `LLMTransform` import (the proven pattern at `profiles.py:57`). Side benefit: stays outside the `plugins/transforms/llm/*` R5 ceiling (`max_hits: 61`, `config/cicd/enforce_tier_model/plugins.yaml`) and outside `LLMTransform.source_file_hash` (PH3).
2. **Amendment 3 (seed → required capability) does NOT bind in Phase 2.** The pipeline LLM path has NO seed concept anywhere (verified: zero `seed` hits in `plugins/transforms/llm/` and `plugins/infrastructure/clients/`). `seed` exists only Composer-side as `WebSettings.composer_seed` (`web/config.py:167`). The amendment therefore binds in **Phase 3**, where a configured `composer_seed` must add `seed` to a gateway profile's required capabilities. Phase 2 adds no `seed` to the `LLMProvider` protocol. Record this in the Phase 3 plan.
3. **What moves vs stays.** Move (provider-neutral): the profile settings model, the runtime profile dataclass, `validate_profile_alias`, the alias/secret-ref regexes. Stay in web (genuinely web-shaped): `RuntimeWebPluginConfig` (takes `WebSettings`), `OperatorProfileRegistry`, `_LLMProfileResolver`, the Bedrock-guardrail resolver, `LoweredPluginConfig`, availability/local-requirement types.
4. **Naming after the move:** `WebLLMProfileSettings` → `LLMProfileSettings`; `RuntimeWebLLMProfile` → `RuntimeLLMProfile`. Flag-day rename at every site (15 `src/` importers, 33 test files). No aliases, no re-exports for compatibility (pre-release no-tech-debt posture).

## Global Constraints

- **No compatibility shims.** After the move there is exactly one definition and one import path per model. `git grep WebLLMProfileSettings` and `git grep RuntimeWebLLMProfile` must return zero hits outside the changelog.
- Gateway profile fields per the design: `provider` (exactly `gateway`), `model` (logical alias), `credential_scope` (exactly `server` in v1), `credential_ref`, `endpoint` (must end `/v1`), `contract_major` (int, initially 1), `required_capabilities` (closed set from `text tools json_object json_schema seed usage`), `timeout_seconds`, `max_tokens`.
- Endpoint rule: HTTPS required EXCEPT the exact loopback form `http://127.0.0.1:<port>/v1`. Reject userinfo, query, fragment, ambiguous host syntax, and any path outside the versioned base. Redirects never followed across origins.
- **Every new gateway field must be added to `_LLM_PRIVATE_OPTIONS`** (`web/plugin_policy/profiles.py:219`) — otherwise `get_config_schema()`'s `$defs` union silently exposes `endpoint`, `credential_ref`, `contract_major`, `required_capabilities` as web-authorable knobs. This is a security requirement, not hygiene.
- ELSPETH owns ALL retry/pooling/row-level policy. The gateway's own single OAuth-refresh replay is the only retry inside it. The provider adds no retry loop.
- Usage: if a profile requires the `usage` capability and the response omits usage, that is a NON-retryable invalid-response failure. If not required and absent, record `TokenUsage.unknown()` — never zeros.
- Gateway error codes map per the design's table: invalid_request/contract_mismatch/model_not_allowed/capability_unsupported → non-retryable config failure; context_length_exceeded → `ContextLengthError`; content_policy_rejected → `ContentPolicyError`; upstream_rate_limited → `RateLimitError`; upstream_timeout/upstream_unavailable/oauth_token_unavailable → retryable (`NetworkError`/`ServerError`); upstream_unauthorized/upstream_response_invalid/internal_error → non-retryable `LLMClientError`.
- ELSPETH never parses raw agency error bodies — only the stable gateway envelope (`error.code`, `error.retryable`, `error.request_id`).
- Full `pytest tests/` is the gate at phase close; `elspeth-lints check` must exit 0 (expect to re-pin the R5 `max_hits` to the EXACT new level if the gateway provider adds isinstance hits — ratchet, not budget).
- Commit by pathspec only; ruff before every commit; no venv mutation.

## Regression surface to keep green

`tests/unit/plugins/llm/` (30 files, heaviest `test_transform.py` 121, `test_llm_config.py` 111, `test_config_schema.py` 10), `tests/unit/web/plugin_policy/test_profiles.py` (22), `tests/integration/web/test_plugin_policy_end_to_end.py` (14), plus 33 test files importing `OperatorProfileRegistry`.
**Golden files that WILL rotate:** `tests/golden/web/catalog/knob_schema/transform__llm.json` and `tests/golden/web/catalog/policy_view/transform__llm.json` (both derive from `get_config_schema()` / `public_schema()`).
**Corpus hash risk:** a new top-level `ElspethSettings` field rotates `semantic_settings_sha256` across all 13 DAG corpus scenarios. Mitigation precedent (commit 32fe59820): add the field to the post-pin drop list at `tests/fixtures/dag_scenario_corpus/harness.py:441-453` — note the existing check is `== []`, so a Mapping-typed catalog needs an `== {}` comparison or the guard silently does nothing.

---

### Task 1: Extract the provider-neutral profile models to core (flag-day)

**Files:** Create `src/elspeth/core/llm_profiles.py`; modify `src/elspeth/web/plugin_policy/profiles.py`; update all importers.

**Interfaces produced:** `LLMProfileSettings` (all current `WebLLMProfileSettings` fields + validators, provider allowlist still via lazy `LLMTransform` import), `RuntimeLLMProfile` (frozen slots dataclass, `from_settings`), `validate_profile_alias`, `PROFILE_ALIAS_PATTERN`, `SECRET_REF_PATTERN`.

- [ ] **Step 1: Move with no behaviour change.** Cut the models into the new module verbatim (renamed), leave `profiles.py` importing them from core. Run `pytest tests/unit/web/plugin_policy/ tests/unit/plugins/llm/ -q` — must be green with ZERO test edits. This proves the move is behaviour-preserving before any rename churn.
- [ ] **Step 2: Flag-day rename.** Rename `WebLLMProfileSettings`→`LLMProfileSettings`, `RuntimeWebLLMProfile`→`RuntimeLLMProfile` at every site. Update the 15 `src/` importers and all test importers. Verify `git grep -n "WebLLMProfileSettings\|RuntimeWebLLMProfile" -- src tests` returns nothing.
- [ ] **Step 3:** `pytest tests/unit/web/ tests/unit/plugins/llm/ tests/integration/web/ -q` green.
- [ ] **Step 4: Commit** — `refactor(llm): move provider-neutral profile models to core.llm_profiles`

---

### Task 2: GatewayConfig

**Files:** Create `src/elspeth/plugins/transforms/llm/providers/gateway.py` (config only); modify `base.py:67` + docstring; modify `transform.py` `_PROVIDERS`, docstring table (~1138), `on_start` limiter chain (~1568), `_create_provider` (~1615).

**Interfaces produced:** `GatewayConfig(LLMConfig)` — `provider: Literal["gateway"] = "gateway"`, `model: str` (logical alias, 1..512), `endpoint: str` (validated; must end `/v1`), `credential_ref: str` (matches `SECRET_REF_PATTERN`), `contract_major: int = 1`, `required_capabilities: tuple[str, ...] = ()` (validated against the closed set), `timeout_seconds: float = 60.0`, `max_tokens: int | None`, `VALUE_SOURCES: ClassVar = ()`, `tracing: dict | None`.

- [ ] **Step 1: Failing tests** — endpoint accept/reject matrix (https ok; `http://127.0.0.1:8787/v1` ok; `http://localhost:.../v1` reject; userinfo/query/fragment reject; path not ending `/v1` reject); unknown capability name reject; `contract_major` must be a supported int; registry contains `gateway`; `get_config_schema()` emits the 4th variant; limiter chain selects a gateway limiter (NOT openrouter's by fallthrough).
- [ ] **Step 2–4:** RED → implement → GREEN.
- [ ] **Step 5: Golden refresh** — regenerate the two `transform__llm.json` goldens; inspect the diff and confirm ONLY additive gateway entries appear.
- [ ] **Step 6: Commit** — `feat(llm): register the gateway provider config`

---

### Task 3: GatewayLLMProvider

**Files:** `providers/gateway.py` (provider class); tests `tests/unit/plugins/llm/test_provider_gateway.py`.

**Interfaces consumed:** `AuditedHTTPClient`; the typed error hierarchy; `TokenUsage.from_dict`; `parse_finish_reason`; `LLMQueryResult`.

- [ ] **Step 1: Failing tests** (respx): happy text path → `LLMQueryResult` with preserved model alias/usage/finish_reason; `response_format` json_object and json_schema forwarded unchanged; contract header `X-ELSPETH-LLM-Gateway-Contract: 1` sent and a mismatched response header rejected; every gateway error code mapped to the correct typed exception with correct retryability; `usage` required-but-absent → non-retryable; usage absent and not required → `TokenUsage.unknown()`, never zeros; no raw upstream/agency text in any exception message (assert a body sentinel never appears); `close()` releases HTTP resources; the TWO audit rows are recorded (transport + semantic `CallType.LLM` with `resolved_prompt_template_hash`).
- [ ] **Step 2–4:** RED → implement → GREEN. `execute_query` posts to `{endpoint}/chat/completions` with the static bearer from the resolved secret ref, contract header, and the canonical message list; validates the response envelope before constructing `LLMQueryResult`; maps errors from the envelope's `code` only.
- [ ] **Step 5: `runtime_preflight`** — first GET `{endpoint%/v1}/readyz` (contract major, adapter identity, model alias present, required capabilities ⊆ declared), THEN one bounded real completion. A readiness document alone is NOT accepted as health (design requirement). Test both halves, including "readyz ok but completion fails → preflight fails".
- [ ] **Step 6: Commit** — `feat(llm): add the gateway LLM provider`

---

### Task 4: Gateway profile support in the shared profile model

**Files:** `src/elspeth/core/llm_profiles.py`; `src/elspeth/web/plugin_policy/profiles.py` (`_LLM_PRIVATE_OPTIONS`, `RuntimeLLMProfile.from_settings` provider table, `_LLMProfileResolver`).

- [ ] **Step 1: Failing tests** — a `provider: gateway` profile validates (this currently CRASHES: `from_settings`'s hard-coded `provider_fields[...]` raises `KeyError`, and the settings validator's else-branch rejects `endpoint` for non-azure); `credential_scope` must be `server`; lowering produces the private executable options (provider/model/endpoint/contract_major/required_capabilities/timeout + `api_key` secret ref) and an audit-safe projection containing ONLY `{"profile": alias, ...safe options}`; every gateway private field is rejected if supplied in web-authored `safe_options`; `public_schema()` does NOT expose endpoint/credential_ref/contract_major/required_capabilities.
- [ ] **Step 2–4:** RED → implement → GREEN. Rewrite the settings validator's provider branches so `gateway` has its own arm rather than falling through the azure/openrouter/bedrock else-chain.
- [ ] **Step 5: Commit** — `feat(llm): support gateway profiles in the shared profile contract`

---

### Task 5: Batch/CLI profile catalog on ElspethSettings

**Files:** `src/elspeth/core/config.py` (`ElspethSettings`); a lowering pass mirroring the web seam; `tests/fixtures/dag_scenario_corpus/harness.py` (post-pin drop list).

- [ ] **Step 1: Failing tests** — `llm_profiles` catalog + `default_llm_profile` parse; an `llm` transform node with `{"profile": "alias"}` and no `provider` lowers to the same private executable options the web path produces for the same profile (the design's acceptance criterion: "Web and batch profile aliases lower to identical private gateway bindings and audit-safe projections" — assert equality of the two lowered dicts directly); unknown alias / unsupported provider / missing server secret / unsupported contract major / capability not reported by the gateway all fail closed at config time; existing explicit azure/openrouter/bedrock node configs still work unchanged.
- [ ] **Step 2–4:** RED → implement → GREEN.
- [ ] **Step 5: Corpus hash guard** — add the new field(s) to `harness.py`'s post-pin drop list using a `== {}` comparison for the Mapping. Run `pytest tests/integration/core/dag/test_dag_scenario_production_path.py -q` and confirm ZERO pin rotations. If pins rotate, STOP and report — do not re-pin.
- [ ] **Step 6: Commit** — `feat(config): operator LLM profile catalog for batch and CLI`

---

### Task 6: End-to-end against the Phase 1 reference gateway

**Files:** `tests/integration/plugins/llm/test_gateway_provider_e2e.py`.

- [ ] **Step 1:** Stand up the Phase 1 in-process gateway (reuse the pattern in `gateway/conformance/conftest.py`: one `httpx.AsyncClient` with a host-routing transport over the mock OAuth + mock upstream ASGI apps) and drive the real `GatewayLLMProvider` against it.
- [ ] **Step 2:** Assert the design's acceptance criteria 4, 5, 6, 9: single-query text and multi-query JSON Schema both work; usage and finish reason preserved and never invented; ELSPETH retries stay outside the gateway (assert the provider issues exactly one HTTP call per query — no internal retry loop); contract/capability/response-shape/timeout/sanitized-error failures map to the correct existing ELSPETH outcomes.
- [ ] **Step 3: Commit** — `test(llm): end-to-end gateway provider against the reference stack`

---

### Task 7: Phase close

- [ ] Full `pytest tests/` green (CI-equivalent selection).
- [ ] `elspeth-lints check` exit 0. If the R5 `plugins/transforms/llm/*` ceiling trips, re-pin `max_hits` to the EXACT observed level (ratchet discipline) and note the delta in the commit message. Do NOT touch signed entries or `judge_metadata_signature`.
- [ ] PH3: `LLMTransform.source_file_hash` (`transform.py:1154`) is stale after the registry edit — refresh it with the plugin hash fixer (CI-only gate, will not fire locally).
- [ ] Update `docs/superpowers/plans/2026-07-30-llm-gateway-master-plan.md` Phase 2 row to reflect actual delivery, and write the Phase 3 plan (carrying amendment 3: `composer_seed` set → `seed` in required capabilities; and amendment 2: disposition `src/elspeth/web/_aws_ecs_acceptance/bedrock.py`).
