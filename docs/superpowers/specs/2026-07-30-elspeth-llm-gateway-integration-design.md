# ELSPETH LLM Gateway Integration and AWS Scenario C Design

**Status:** Approved design
**Date:** 2026-07-30
**Decision owner:** Project developer
**Related design:** [LLM Compatibility Gateway Runtime](2026-07-30-llm-compatibility-gateway-runtime-design.md)

## Summary

Add `gateway` as an optional first-class ELSPETH LLM provider for the runtime product surfaces: the Web Composer and the pipeline `llm` transform. Operators bind opaque profiles to a compatibility-gateway URL, static bearer secret reference, logical model alias, contract major, timeout, and required capabilities. Web-authored pipelines and Composer prompts see profile aliases, never endpoints or credential references.

Add AWS Terraform Scenario C as a maintained cold-install scenario: Scenario A's AWS/PostgreSQL deployment shape with a custom-gateway sidecar replacing direct Bedrock LLM access. The gateway is an essential container in the same Fargate task, listens only on loopback, and is independently pinned and admitted before the task receives credentials.

Existing direct Azure OpenAI, OpenRouter, and Bedrock providers remain supported and retain their current behavior.

## Current state

At design commit `94aaddba5e59242a1916193a8e2360f29bcc8229`:

- Composer funnels many paths through `_litellm_acompletion`, but primary, advisor, guided-chat, planner, boot-probe, and automatic-title callers still construct LiteLLM-shaped arguments around direct model identifiers.
- The pipeline `llm` transform has one provider registry containing `azure`, `openrouter`, and `bedrock`, with a narrow `LLMProvider` protocol for execution, runtime preflight, and close.
- Pipeline LLM execution already owns pooling, row-level retry, text and multi-query strategies, JSON object and JSON Schema response formats, usage, finish-reason checks, and audited external calls.
- `WebLLMProfileSettings` and `OperatorProfileRegistry` already lower opaque web profile aliases into private provider options, but the settings and validation are web-owned and shaped only for the three current providers.
- AWS Terraform Scenarios A and B restrict Composer models and web LLM profiles to `bedrock/...`, derive Bedrock IAM grants from those models, and place only CloudWatch Agent and ELSPETH Web in the service task.
- The shared Terraform module infers first-install versus upgrade behavior from the scenario letter, which cannot represent a new cold-install backend independently.

This design extends those seams rather than creating a parallel authoring registry or replacing direct providers.

## Goals

- Route every runtime Composer LLM path through either the existing direct transport or the compatibility gateway, selected by operator configuration.
- Add a gateway implementation of the existing pipeline `LLMProvider` contract.
- Reuse one operator-owned LLM profile model across web lowering and batch/CLI execution.
- Keep endpoints, bearer-secret references, adapter identity expectations, and upstream mappings out of web-authored pipeline documents.
- Preserve existing Landscape audit authority, retry ownership, structured-output behavior, and direct-provider compatibility.
- Ship a maintained Scenario C that proves deployment, secrets, provenance, Composer tools, pipeline structured output, and failure behavior.
- Make gateway availability and identity visible to operators without exposing endpoints or secrets.

## Non-goals

- Removing or routing the existing direct providers through the gateway.
- Routing evaluation, lint-judge, or release-signing model calls through gateway profiles.
- Letting web authors submit arbitrary model names, endpoints, credentials, contract versions, or adapter options.
- Supporting user-scoped gateway bearer credentials in the first release.
- Treating gateway readiness as proof that the agency upstream is healthy; real preflight and calls remain authoritative.
- Embedding the gateway runtime or an agency adapter in the ELSPETH Web image.
- Including a real agency adapter or live agency acceptance in Scenario C.
- Making Scenario C an upgrade/OIDC scenario; it is a cold-install sibling of Scenario A.

## Target architecture

```text
                         operator-owned LLM profiles
                                      |
                   +------------------+------------------+
                   |                                     |
                   v                                     v
          Web Composer resolver                  pipeline profile lowering
                   |                                     |
          ComposerCompletionClient                    LLMProvider
                   |                                     |
                   +------------------+------------------+
                                      |
                         Gateway HTTP client boundary
                                      |
                         /v1/chat/completions
                                      |
                     elspeth-llm-gateway sidecar
                                      |
                     OAuth2 + custom /v1/invoke adapter
```

The profile registry is the policy boundary. A profile binds one stable alias to one provider and one logical model. Authors select the alias. The registry lowers it to private executable configuration and produces a separate audit-safe projection.

## Shared LLM profile contract

Move the provider-neutral LLM profile models and validation out of the web-only module into the LLM domain. Web settings and core/batch settings consume the same models; compatibility imports may remain temporarily to avoid a flag-day migration.

A gateway profile contains:

| Field | Rule |
|---|---|
| `provider` | Exactly `gateway`. |
| `model` | One logical alias published by the gateway. |
| `credential_scope` | Exactly `server` in v1. |
| `credential_ref` | Operator secret-store reference for the static inbound bearer. |
| `endpoint` | Operator-owned gateway base ending in `/v1`. |
| `contract_major` | Required supported integer, initially `1`. |
| `required_capabilities` | Closed set from the gateway contract. |
| `timeout_seconds` | Positive bounded end-to-end request timeout. |
| `max_tokens` | Optional operator ceiling, retaining existing profile semantics. |

HTTPS is required except for the exact loopback form `http://127.0.0.1:<port>/v1`, which exists for same-task sidecars and local development. URLs with userinfo, query, fragment, ambiguous host syntax, or a path outside the versioned base are rejected. Redirects are not followed across origins.

An illustrative operator configuration is:

```yaml
llm_profiles:
  composer-primary:
    provider: gateway
    model: standard
    credential_scope: server
    credential_ref: LLM_GATEWAY_BEARER_TOKEN
    endpoint: http://127.0.0.1:8787/v1
    contract_major: 1
    required_capabilities: [text, tools, usage]
    timeout_seconds: 60
  composer-advisor:
    provider: gateway
    model: fast
    credential_scope: server
    credential_ref: LLM_GATEWAY_BEARER_TOKEN
    endpoint: http://127.0.0.1:8787/v1
    contract_major: 1
    required_capabilities: [text, tools, usage]
    timeout_seconds: 60
  pipeline-structured:
    provider: gateway
    model: standard
    credential_scope: server
    credential_ref: LLM_GATEWAY_BEARER_TOKEN
    endpoint: http://127.0.0.1:8787/v1
    contract_major: 1
    required_capabilities: [text, json_schema, usage]
    timeout_seconds: 60

composer_profile: composer-primary
composer_advisor_profile: composer-advisor
default_llm_profile: pipeline-structured
```

The names are examples, not required literals. Profile aliases and model aliases are audit-safe operator identifiers; endpoints and credential references remain private. The same bearer reference may back several profiles without copying the secret.

### Web-authored and batch configuration

The web public schema continues to expose only `profile` plus safe prompt/row behavior. Profile lowering injects provider, logical model, endpoint, secret reference, contract, timeout, and capability requirements into the private executable configuration. The stored authoring document and Composer context retain only the alias.

For CLI/batch use, `ElspethSettings` gains a top-level operator profile catalog and the `llm` transform accepts a profile selector as an alternative to its legacy explicit provider configuration. The top-level catalog is operator configuration; transform nodes remain portable and contain no endpoint or credential. Secret values still come only from the configured secret source. Existing explicit Azure/OpenRouter/Bedrock pipeline configuration remains valid.

Configuration fails closed for an unknown alias, unsupported provider, unsupported contract major, malformed endpoint, absent server secret, invalid capability name, or profile whose required capabilities are not reported by the gateway.

## Pipeline provider integration

Add `GatewayConfig` and `GatewayLLMProvider` to the existing LLM provider registry. They implement the current `LLMProvider` protocol rather than introducing a second transform or strategy system.

The provider:

- converts existing canonical pipeline messages to the contract-major-1 request;
- carries `response_format` unchanged for JSON object and JSON Schema strategies;
- preserves configured temperature, seed when present, maximum-token behavior, and logical model alias;
- validates the gateway response before constructing `LLMQueryResult`;
- preserves returned model alias, finish reason, actual usage, and request ID;
- maps gateway codes to existing `RateLimitError`, `NetworkError`, `ServerError`, `ContentPolicyError`, `ContextLengthError`, configuration errors, or non-retryable `LLMClientError` as appropriate; and
- closes its HTTP resources through the existing provider lifecycle.

The external request/response audit boundary stays in an audited client owned with the provider infrastructure. Transform strategies consume validated `LLMQueryResult` values and do not perform HTTP, handle secrets, or manufacture audit records. Pooling, dispatch delay, retry budget, and row-level decisions remain owned by the existing transform. The gateway itself performs only its one OAuth-refresh replay.

`runtime_preflight` first checks gateway readiness, contract major, adapter identity, model alias, and capabilities, then performs one bounded authenticated real model request. A readiness document alone is not accepted as upstream health. Structured-output and tool behavior are proven in acceptance tests rather than by pretending a text probe covers every feature.

If a profile requires `usage`, absent usage is a non-retryable invalid-response failure. If usage is not required and unavailable, ELSPETH records explicit unavailability; it never records invented zeros.

## Composer integration

Introduce an injected `ComposerCompletionClient` boundary with two implementations:

- a direct implementation preserving the current LiteLLM behavior; and
- an asynchronous gateway implementation using the same strict contract and error mapping as the pipeline provider.

All runtime Composer LLM paths must resolve through that boundary: primary tool loop, advisor/escape-hatch calls, guided chat, guided and freeform planners, boot probe, automatic title, and any helper that currently imports `_litellm_acompletion`. Retaining one unreviewed direct helper path would make the deployment mode incomplete.

`composer_profile` and `composer_advisor_profile` are optional operator settings. For each role, a profile selector takes authority when present; otherwise the existing direct `composer_model` or `composer_advisor_model` remains authoritative. A selected profile must be server-scoped and suitable for that role. The resolved primary and advisor identities must remain distinct, preserving the existing two-model independence rule.

The gateway client supports Composer message history, tool definitions, tool choice, assistant tool calls, tool results, text completions, timeout/cancellation, and returned model identity. The surrounding Composer services continue to own tool admission, tool execution, turn authority, buffered Landscape audit settlement, token budgeting, and cancellation. Gateway integration changes transport, not Composer authority.

Boot probing checks both primary and advisor bindings. A gateway binding verifies readiness and performs an actual minimal completion; a direct binding retains its current probe. Mixed direct/gateway role configurations are permitted, although Scenario C configures both roles through gateway profiles.

## Error, retry, and cancellation behavior

ELSPETH maps gateway errors as follows:

| Gateway class | ELSPETH behavior |
|---|---|
| Invalid request, contract, model, or capability | Configuration/non-retryable failure |
| Context length | Existing context-length failure |
| Content policy | Existing content-policy failure |
| Rate limit | Existing retryable rate-limit failure |
| Timeout/network/upstream unavailable | Existing retryable transport/server failure |
| OAuth temporarily unavailable | Retryable server failure subject to ELSPETH budget |
| Upstream unauthorized after refresh | Non-retryable provider/authentication failure |
| Invalid normalized response | Non-retryable provider-response failure |

ELSPETH never parses raw agency error bodies. It consumes only the stable gateway envelope. Client cancellation closes or abandons the in-flight HTTP request within a bounded period and does not retry. Composer and transform retries continue to produce their existing audit events; the gateway's internal OAuth refresh is recorded as metadata on the same external-call cohort, not as a second LLM decision.

## Audit and operator surfaces

Landscape remains the authoritative content audit. For every gateway-backed call, ELSPETH records the same prompt/response/tool/usage/finish data it records for a direct provider plus safe forensic metadata:

- profile alias and logical model alias;
- requested and returned model identity;
- gateway contract major;
- gateway request ID;
- adapter name, version, API major, and package fingerprint;
- gateway model-mapping generation; and
- canonical request/response hashes used for correlation.

Endpoints, secret-reference names, bearer values, OAuth values, and raw gateway/upstream errors do not enter row data, public status, Composer context, or ordinary logs. Business output fields remain controlled by the transform strategy; gateway forensic fields belong in success-reason metadata and external-call audit records.

`/api/system/status`, Composer availability, doctor, and audit-readiness surfaces report:

- selected profile alias;
- provider `gateway` or the existing direct provider;
- operational availability per primary/advisor role;
- contract major;
- safe adapter identity and mapping generation; and
- missing capability or sanitized failure code when unavailable.

They do not report gateway URLs, credential references, token state, or upstream origins. `/api/ready` retains its existing service-readiness meaning; remote LLM health remains a separate status fact.

## AWS Terraform Scenario C

Create `deploy/aws-ecs/terraform/scenario-c/` as a maintained cold-install sibling of Scenario A. Scenario C uses the shared module with two explicit axes:

- deployment lifecycle: `first` or `upgrade`; and
- LLM backend: `bedrock` or `custom_gateway`.

Scenario A is `first + bedrock`, Scenario B is `upgrade + bedrock`, and Scenario C is `first + custom_gateway`. Scenario-letter conditionals no longer infer either concern implicitly.

Scenario C has its own backend example, variable example, codeblind compatibility data, outputs, inventory bindings, tests, runbook instructions, and acceptance evidence. It must be runnable without editing shared-module source.

### Service task topology

The Scenario C service task contains:

```text
cloudwatch-agent       non-essential
elspeth-llm-gateway    essential, loopback :8787
elspeth-web            essential, public application :8451
```

Fargate `awsvpc` containers in one task share the network namespace, so Web calls the gateway through `http://127.0.0.1:8787/v1`. The gateway binds only to loopback and has no port mapping. Static bearer authentication remains mandatory even on loopback.

The gateway health check calls `/readyz`. Web depends on both CloudWatch Agent and gateway `HEALTHY`; it must not start before the gateway validates configuration and adapter loading. The gateway is essential, so its exit makes the task unhealthy and ECS replaces the complete unit. Web's own health remains `/api/health`.

The gateway container:

- runs non-root with a read-only root filesystem;
- has no EFS mount and no access to ELSPETH payload, session, or Landscape storage;
- receives separate CPU and memory reservations and bounded process limits;
- uses the existing AWS log driver but emits metadata-only logs;
- receives only the static inbound bearer, OAuth client ID/secret, and gateway-specific configuration; and
- has HTTPS egress to the configured token and invoke origins.

The task CPU/memory total is increased explicitly rather than overcommitting Scenario A's existing allocation.

### Secrets

Terraform accepts Secrets Manager selectors or ARNs for:

- the static ELSPETH-to-gateway bearer, injected into both Web and gateway under their respective environment names;
- OAuth client ID, injected only into the gateway; and
- OAuth client secret, injected only into the gateway.

Terraform never accepts the literal values and does not create or rotate them. Existing selector validation, exact-version admission, non-leaking plan outputs, and execution-role access checks apply. OAuth secrets are absent from Web; ELSPETH database and application secrets are absent from the gateway.

Scenario C's profile JSON targets loopback and references the Web-side bearer name. Secret names and values are omitted from public outputs and retained acceptance evidence.

### IAM and network policy

`custom_gateway` mode omits all Bedrock model and inference-profile permissions and does not require Bedrock ARN variables. The ECS task role retains only permissions needed by the ordinary ELSPETH deployment; the execution role gains the exact gateway-image and gateway-secret access required at task launch.

Gateway HTTPS egress must be possible through the deployment network. Scenario C documents required destinations without baking an agency hostname into the module. Security-group and route behavior stays fail-closed to the deployment's explicit egress policy. No inbound security-group rule exposes the gateway port.

### Image admission and identity

Scenario C accepts independently pinned ELSPETH Web and agency-derived gateway image digests. Before any credential-bearing service task definition is registered, the deployment verifies for the gateway image:

- approved ECR account, region, repository, and immutable digest;
- repository provenance and admission status equivalent to the Web candidate image;
- acceptable scan status and architecture;
- supported gateway contract major;
- expected adapter name, version, API major, and package fingerprint; and
- expected base gateway runtime identity.

The verified identities are bound into the task definition, deployment inventory, and sanitized acceptance evidence. A mutable tag, unverified adapter package, identity mismatch, or unavailable scan blocks before secret injection. Scenario C must not weaken the candidate-image provenance gate merely because the gateway is a sidecar.

### Doctor, rollback, and acceptance tasks

Any one-off task that exercises LLM health or behavior launches the same pinned gateway sidecar and secret/config bindings as the service task. In particular, runtime doctor and live acceptance cannot probe a host-side or substitute gateway. Schema initialization and storage-only tasks need not start the sidecar when they make no LLM call.

Scenario C rollback definitions include the compatible pinned gateway sidecar and profile contract. Rollback preflight verifies that the selected Web rollback image supports gateway contract major 1 and that its expected profile schema matches the deployed sidecar. Rolling Web back to an image that only understands direct Bedrock is rejected before service mutation.

## Acceptance criteria

### Core integration

1. Existing Azure, OpenRouter, and Bedrock unit and integration behavior remains green.
2. Web and batch profile aliases lower to identical private gateway bindings and audit-safe projections.
3. Raw endpoints and credential references are rejected from web-authored `llm` node options.
4. Pipeline single-query text and multi-query JSON Schema modes work through the reference gateway.
5. Usage and finish reason are preserved, and unavailable usage is never invented.
6. Pooling and ELSPETH retries remain outside the gateway; only one OAuth-refresh replay occurs inside it.
7. Every Composer LLM caller, including title and boot paths, can run with direct or gateway bindings without a hidden LiteLLM-only path.
8. Composer completes a tool-call/tool-result round trip through the reference gateway.
9. Gateway contract, capability, response-shape, cancellation, timeout, and sanitized error failures map to the correct existing ELSPETH outcomes.
10. Landscape contains the content audit and safe gateway identity, while application and gateway logs contain no content or secrets.

### Terraform and deployment

1. Scenarios A and B retain their Bedrock behavior after shared-module generalization.
2. Scenario C plans as `first + custom_gateway` and requires no Bedrock model or ARN input.
3. Gateway image provenance and adapter identity are verified before any task definition receives gateway or OAuth secrets.
4. The service task contains exactly the intended CloudWatch, gateway, and Web containers; gateway is essential, private, non-root, read-only, and unmounted from EFS.
5. Web waits for gateway readiness, and gateway failure causes ECS to replace the task.
6. Web can reach the gateway only through loopback and no gateway port is exposed by the task or security group.
7. OAuth credentials are available only to the gateway; ELSPETH application/database credentials are unavailable to it.
8. Runtime doctor and live acceptance use the identical pinned sidecar identity and model mappings.
9. A live reference-stack acceptance proves Composer tool use, pipeline text, pipeline structured multi-query, usage/finish/audit metadata, and sanitized failure cases.
10. Inventory and evidence identify both image digests, contract, adapter fingerprint, mapping generation, and exact secret versions without disclosing secret names or values.

## Delivery sequence

The implementation plan should preserve this dependency order:

1. Publish and conformance-test gateway contract major 1, SDK, reference adapter, and local mock stack.
2. Add shared ELSPETH profile types and strict gateway HTTP client contracts.
3. Add the pipeline provider with focused audit and error tests.
4. Add the Composer client boundary and migrate every caller.
5. Generalize the Terraform shared module without changing A/B behavior.
6. Add Scenario C, sidecar provenance/secrets/topology, and one-off task parity.
7. Run local reference acceptance, full ELSPETH verification, Terraform tests, container tests, and disposable AWS acceptance.

The gateway contract must exist before ELSPETH integration is treated as stable. Scenario C must not accept a placeholder image or bypass admission while the reference runtime is unfinished.

## Alternatives considered

### Replace all direct providers with the gateway

Rejected because direct providers are working deployment options and forcing an additional service would add operational cost and a new failure mode for installations that do not need schema translation or OAuth.

### Add gateway support only to Composer

Rejected because web-authored pipeline LLM nodes and direct pipeline execution may use the same agency route. Composer-only transport would create a misleading partial deployment.

### Add gateway support only to the pipeline provider registry

Rejected because Composer's direct LiteLLM helpers would still bypass the selected agency route.

### Put gateway fields directly in authored nodes

Rejected because authors must not control egress endpoints, static bearer references, contract versions, or model routing. Opaque operator profiles are the existing policy boundary.

### Deploy the gateway as a separate ECS service

Deferred. A separate service is appropriate for later shared scale or multi-application use, but the first maintained scenario favors one task's loopback isolation, atomic replacement, and smaller operational surface.
