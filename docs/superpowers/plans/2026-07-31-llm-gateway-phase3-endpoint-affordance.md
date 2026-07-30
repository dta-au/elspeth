# Phase 3 (revised): Make the Gateway a Plain OpenAI-Compatible Endpoint

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.
>
> **This plan SUPERSEDES the deleted `2026-07-30-llm-gateway-phase3-composer.md`** (the `ComposerCompletionClient` boundary + nine-caller migration + exception parity sweep). See "Why the scope changed".

## Why the scope changed (operator decision, 2026-07-31)

The original design had ELSPETH learn to speak to the gateway. That was backwards. ELSPETH — via LiteLLM and via its own OpenRouter provider — **already speaks to any OpenAI-compatible endpoint at an arbitrary base URL**, including HTTP loopback (`src/elspeth/plugins/transforms/llm/providers/openrouter.py:207-217`, whose comment explicitly names "local ChaosLLM/OpenAI-compatible dev servers"). The only thing preventing a generic OpenAI client from talking to our gateway is a self-inflicted requirement we added: `X-ELSPETH-LLM-Gateway-Contract` is mandatory on every `/v1/*` request (`gateway/src/elspeth_llm_gateway/core/app.py:130`).

**The boundary is now stated as:** ELSPETH must have a coherent, trusted link to an LLM. *The operator owns providing that link, by whatever means they choose.* The gateway is a supported affordance an agency can bootstrap into a translation layer — **not a universal panacea, and not something ELSPETH is required to know about.**

Consequences:
- The `ComposerCompletionClient` boundary, the migration of nine Composer call sites, the exception parity sweep across three route-level handlers, and the judge-signature churn that came with them are all **cancelled**. Composer needs a settable base URL, not an abstraction layer.
- `GatewayConfig`/`GatewayLLMProvider` (Phase 2) are **retained as an optional richer path** — they buy startup capability preflight and exact envelope error codes, which base-URL pointing cannot. They are no longer *the* path.
- **Custody amendment 2** (`_aws_ecs_acceptance/bedrock.py` disposition) is discharged: with no boundary to migrate onto, that acceptance lane keeps using `_litellm_acompletion` directly, as it should — it deliberately exercises Bedrock specifically.
- **Custody amendment 3** (seed ⇒ `seed` capability) applies only to the gateway-*provider* path. The pipeline has no seed concept at all, and the base-URL path has no capability negotiation, so today the rule is vacuous. Restated below so it is not silently lost if `seed` is ever added to the pipeline.

## Global Constraints

- **Do not break existing clients.** The contract header becomes *optional*, not removed: absent ⇒ proceed; present ⇒ must match `CONTRACT_MAJOR` or fail `contract_mismatch` exactly as today. Version negotiation is preserved for clients that opt in.
- No new ELSPETH abstraction layers. Composer changes are configuration surface only.
- Secrets follow the existing path (`${VAR}` for batch/CLI, the secret store for web). No new secret mechanism.
- The operator-owns-the-endpoint risk must be stated plainly in operator-facing docs (see Task 4). This is a real transfer of responsibility and must not be buried.
- Full `pytest tests/` is the phase gate; `elspeth-lints check` exit 0; commit by pathspec; ruff + mypy before every commit; no venv mutation.
- Judge-signed tier-model fingerprints may still drift on any touched file. **Record drift; never sign, never hand-edit a `judge_metadata_signature`** ([O1] custody rule).

---

### Task 1: Make the gateway speakable by a plain OpenAI client

**Files:** `gateway/src/elspeth_llm_gateway/core/app.py`; `gateway/tests/test_app.py`; `gateway/conformance/test_contract.py`; `gateway/README.md`.

- [ ] **Step 1: Failing tests.** A request to `/v1/chat/completions` with a valid bearer and **no** contract header succeeds (200). A request with the header present and equal to `1` still succeeds. A request with the header present and wrong (`2`, `abc`, empty) still fails `contract_mismatch` 400. Every response — success and error — still carries the contract header on the way out (unchanged). Auth is still enforced independently of the header (no-bearer + no-header ⇒ 401, not 200).
- [ ] **Step 2: Run → RED. Step 3: Implement** — in `ContractHeaderMiddleware`, treat a missing header as acceptable and only reject a present-and-mismatched one. **Step 4: GREEN.**
- [ ] **Step 5: Conformance kit** — update the contract tests to assert the new three-way behaviour (absent ok / matching ok / mismatched rejected). This is the agency-facing qualification kit, so the relaxation must be visible there.
- [ ] **Step 6: README** — document that the gateway is a plain OpenAI-compatible endpoint: any OpenAI client can point at `<base>/v1` with the static bearer; the contract header is optional and only used for version negotiation.
- [ ] **Step 7: Commit** — `feat(gateway): accept plain OpenAI clients by making the contract header optional`

### Task 2: Give Composer a settable endpoint

**Files:** `src/elspeth/web/config.py`; `src/elspeth/web/composer/service.py` (kwargs construction only — NOT a boundary); `src/elspeth/web/composer/boot_probe.py`; tests under `tests/unit/web/composer/`.

`composer_model` is currently just a model string with nowhere to put an endpoint (`web/config.py:159`). Add the minimum configuration surface so an operator can point Composer at any OpenAI-compatible endpoint.

- [ ] **Step 1: Decide and state the shape** before coding: one shared endpoint setting or per-role (primary/advisor)? Per-role is likely right — the two-model independence rule (`web/config.py:808-828`) means the roles are deliberately separable, and an operator may run the advisor direct while the primary goes through a gateway. Record the choice and why in the task report.
- [ ] **Step 2: Failing tests** — when the endpoint setting is unset, the kwargs passed to `litellm.acompletion` are byte-identical to today (this is the no-regression guarantee, and it must be asserted, not assumed). When set, `api_base` and the resolved bearer are passed through, for the primary role, the advisor role, the planner surfaces, the guided step solvers, the boot probe, and auto-title. The bearer comes from the existing secret path and never appears in a log, an error, or an audit record.
- [ ] **Step 3–4:** RED → implement → GREEN. Touch only kwargs construction; do NOT introduce a client abstraction.
- [ ] **Step 5: Boot probe** — it must probe whatever endpoint is configured for each role, so a misconfigured endpoint fails at boot rather than at first user turn. Preserve today's semantics: `BadRequestError` ⇒ fatal `ComposerBootConfigError`; transient/timeout ⇒ warn and continue.
- [ ] **Step 6: Commit** — `feat(web): allow Composer to target any OpenAI-compatible endpoint`

### Task 3: Prove it end to end against the real gateway

**Files:** `tests/integration/web/composer/test_composer_against_gateway.py` (new).

- [ ] Reuse the Phase 2 e2e harness pattern (`tests/integration/plugins/llm/test_gateway_provider_e2e.py`): a real uvicorn server on an ephemeral loopback port running the real gateway app with the reference adapter + mock OAuth + mock upstream. Point Composer's new endpoint setting at it.
- [ ] Prove a **tool-call / tool-result round trip** through Composer against the gateway (design acceptance criterion 8) — this is the criterion that proves the conversation actually works, and it is the whole point of the phase.
- [ ] Prove the freeform loop's attribute-access consumption is satisfied by what LiteLLM returns from the gateway (`response.choices[0].message.tool_calls[i].function.name` etc.). **If it is not, STOP and report BLOCKED** — that would mean the gateway's response shape needs fixing, not the test.
- [ ] Assert no gateway/agency text leaks into Composer audit records or logs.
- [ ] **Commit** — `test(web): Composer tool round trip against the reference gateway`

### Task 4: Documentation — including the risk transfer

**Files:** `gateway/README.md`; the operator-facing deployment/configuration docs; `docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-integration-design.md` (supersession note).

- [ ] **Step 1: Document both paths.** The *simple* path: point ELSPETH at any OpenAI-compatible endpoint via base URL + bearer — no gateway-specific code involved. The *richer* path: use the `gateway` provider for startup capability preflight and exact envelope error codes. Say plainly when each is appropriate.
- [ ] **Step 2: State the risk transfer, unhedged — and frame it correctly.** The framing is NOT a liability disclaimer. It is a statement about where the system boundary actually lies (operator, 2026-07-31): **ELSPETH is not separate from the model you put behind it.** The pipeline, the validation, and the audit trail are the apparatus around a model; they make what the model did *reviewable, explainable and reproducible* — they do not make it *correct*. ELSPETH's guarantees are about faithful recording and reproduction, not about the quality or honesty of the thing being recorded. A pipeline is only as trustworthy as its weakest link, and the endpoint is a link the operator chooses.
      Name concrete failure modes rather than gesturing at them: a proxy that substitutes a cheaper model; one that truncates or reorders tool calls; one that returns a well-formed response with fabricated content; one that logs prompts you believed were private; one that degrades quietly under load. Then separate honestly what ELSPETH **can** detect — returned model identity is validated and never substituted (a Tier-3 fabrication guard), usage is recorded as unavailable rather than invented, finish reasons are not silently normalised to `stop`, and every call is recorded with request/response hashes for correlation — from what it **cannot**: content fidelity, upstream retention or training use, and silent quality degradation. The audit trail will faithfully record a bad answer as a bad answer; it cannot tell you the answer was bad.
- [ ] **Step 3: Supersession note** in the integration design: the "Composer integration" section's `ComposerCompletionClient` boundary is superseded by this plan; record the date, the reason, and that amendments 2 and 3 are discharged/restated accordingly.
- [ ] **Step 4: Commit** — `docs: document the endpoint affordance and the operator's ownership of it`

### Task 5: Phase close

- [ ] Full `pytest tests/` green (was 33370 at Phase 2 close).
- [ ] `elspeth-lints check` exit 0; record any judge-signed drift without touching it.
- [ ] Update `docs/superpowers/plans/2026-07-30-llm-gateway-master-plan.md`: Phase 3 row rewritten to this scope, and note that Phase 4 (Terraform Scenario C) is unchanged in shape — the sidecar is still a sidecar; ELSPETH just points at it by base URL.
