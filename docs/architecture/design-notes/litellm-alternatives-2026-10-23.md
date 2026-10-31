# LiteLLM alternatives for ELSPETH

Evaluation date: 2026-10-23. Source baseline: `main` at
`3b28d537be98234da8135c418bf185a998c18b68`. This is a recommendation, not an
approved implementation or a release assignment. No dependencies, application
code, cloud resources, or deployed configuration changed during this evaluation.

## Recommendation

Extend ELSPETH's existing gateway and provider boundaries, using native SDKs or
direct HTTP for the required routes. Do not start a new general-purpose LLM
framework. Our gateway already provides a substantial foundation; the remaining
work is provider fidelity, cloud authentication, and removing LiteLLM's ancillary
pricing/catalog dependencies. No percentage-complete or schedule estimate has
been measured.

Use explicit OpenAI-compatible routes where their capabilities meet the workload.
Use native Bedrock Converse and Google Gen AI adapters where compatibility
endpoints lose required information or capabilities. Keep the existing direct
Azure path usable during migration. GCP should initially mean Vertex-hosted
Gemini, with other GCP model families and the Gemini Developer API explicitly
separate scope.

If we want a maintained Python translation library instead, the strongest
shortlist candidate is **Pydantic AI's low-level model layer**, installed through
selected `pydantic-ai-slim` extras. If we want to outsource the gateway service
itself, investigate **Bifrost**. Neither option automatically preserves ELSPETH's
audit and accounting semantics.

## What we already own

Two components matter:

- `gateway/` is the standalone OpenAI Chat Completions-subset service. It has its
  own dependency lock, strict request/response contracts, adapter SDK, OAuth
  token handling, bounded transport, safe errors, readiness and conformance kit.
  It does not depend on LiteLLM or the engine (`gateway/pyproject.toml:7`,
  `gateway/README.md:11`). Its service checks declared capabilities before an
  upstream call (`gateway/src/elspeth_llm_gateway/core/service.py:116`).
- `src/elspeth/web/composer/provider_gateway.py:451` is Composer's internal
  provider-call boundary. It already owns prompt guard, credential checks,
  custody and admission before its physical LiteLLM call at `:569`. The tool
  loop and pipeline authoring do not need replacement.

The engine already has a direct audited HTTP gateway provider, including
replay/verify, governance and logical audit
(`src/elspeth/plugins/transforms/llm/providers/gateway.py:579`, `:699`). Its
preflight checks gateway identity/capabilities and makes a real completion
(`:854`). This existing provider expects ELSPETH's gateway readiness contract;
an arbitrary third-party OpenAI-compatible gateway is not interchangeable under
that configuration without adaptation.

Other existing paths also reduce the work:

- Azure pipeline inference already uses `AzureOpenAI(max_retries=0)` directly
  (`src/elspeth/plugins/transforms/llm/providers/azure.py:334`).
- OpenRouter pipeline inference already sends OpenAI-shaped JSON through
  `AuditedHTTPClient`, rather than delegating inference to LiteLLM
  (`src/elspeth/plugins/transforms/llm/providers/openrouter.py:413`).
- Pipeline Bedrock uses a small LiteLLM adapter under the existing audited LLM
  client (`src/elspeth/plugins/transforms/llm/providers/bedrock.py:181`).
- Owned pipeline results and token usage already exist
  (`src/elspeth/plugins/transforms/llm/provider.py:383`,
  `src/elspeth/contracts/token_usage.py:29`).

These facts support reuse. They do not establish that the current standalone
gateway already has working cloud-provider adapters: its bundled reference
adapter explicitly targets a fictional upstream
(`gateway/src/elspeth_llm_gateway/reference/adapter.py:1`).

## Alternatives

| Option | What it buys | Remaining ELSPETH work and cost | Assessment |
| --- | --- | --- | --- |
| Keep locked LiteLLM | Existing translations, catalog and pricing; least immediate migration work | Continue owning dependency updates and existing behavior workarounds; retains a broad executable dependency | Reasonable holding position while a replacement is qualified |
| Extend our gateway / native adapters | Reuses our contracts, conformance and operational surface; explicit supported routes and translation behavior | Cloud auth, native request/response mapping, richer canonical messages, pricing/catalog and parity qualification | Preferred for the bounded provider scope |
| Pydantic AI low-level models | Maintained Python model/provider abstraction; documented OpenAI, Azure, Bedrock and Google coverage; selectable dependencies | Convert messages/results, preserve native fields, audit schema transforms, inject controlled clients and verify every actual attempt | Best library alternative; use `Model.request`, not a new agent loop |
| Bifrost | Go gateway with documented OpenAI, Azure, Bedrock and Vertex support; Apache-2.0 project | Operate and qualify a different service, integrate identities/errors, control retries/fallback/cache/logging, preserve upstream evidence | Strong external-gateway candidate if we want to stop maintaining translation |
| Portkey gateway | Existing multi-cloud gateway and compatible client surface; public repository identifies an MIT license | Similar service/attempt/logging integration work; select a concrete OSS release and verify the boundary with hosted/enterprise functionality | Viable, but overlaps heavily with what we already own |
| Mirascope | Unified calls and structured model APIs; current compatible-endpoint configuration | Legacy v1 documentation lists Azure/Bedrock, but the current provider page retrieved here does not establish equivalent native coverage | Do not shortlist without version-specific proof of the required cloud routes |

The library/gateway documentation supports these capability claims, not a
security ranking or an ELSPETH integration verdict. No alternative's package
closure was installed or audited, and no advertised throughput or latency claim
was used to rank them. Pydantic AI's model layer is callable independently of its
agent machinery; its model profiles also transform provider-specific schemas,
which means wire-fidelity checks remain necessary.

Sources: [Pydantic AI models/providers](https://pydantic.dev/docs/ai/models/overview/),
[low-level request API](https://pydantic.dev/docs/ai/api/models/base/#pydantic_ai.models.Model.request),
[slim installation](https://pydantic.dev/docs/ai/overview/install/#slim-install),
[Bifrost project](https://github.com/maximhq/bifrost),
[Portkey project](https://github.com/Portkey-AI/gateway),
[Portkey integrations](https://docs.portkey.ai/docs/integrations/llms),
[current Mirascope providers](https://mirascope.com/docs/learn/llm/providers).

## Provider-by-provider in-house design

| Route | Reuse and proposed implementation | Qualification boundary |
| --- | --- | --- |
| OpenAI-compatible | Direct audited HTTP or the existing OpenAI SDK with explicit base URL, timeout and zero automatic retries; retain Chat Completions where that is the chosen contract | Compatibility is a syntax claim, not proof of strict schemas, tool semantics, reasoning or usage fidelity; test each endpoint/model |
| Azure | Retain existing direct SDK path; add gateway support using key or Entra authentication. Modern Azure v1 supports the OpenAI client | Preserve deployment aliases and configured API versions. Do not silently move existing dated-API deployments to v1 or drop Prompt Shield/content-policy detail |
| Bedrock | Prefer existing AWS SDK credential resolution plus Converse for required native features; consider documented OpenAI-compatible endpoints where sufficient | Preserve task-role/default-chain, explicit keys/session tokens and bearer-key modes; prove signing, refresh, model/inference-profile routing, schema support, cache counters and signed reasoning replay |
| GCP / Vertex Gemini | Start with Vertex's OpenAI-compatible endpoint if it carries the needed contract; otherwise use `google-genai` with explicit Vertex project/location/auth | Application Default Credentials and refresh, schema subset, safety refusals, thought signatures, tools and usage require direct qualification. Vertex Claude is a separate native dialect, not implied Gemini support |

Cloud compatibility now reduces the amount of custom translation potentially
needed. Azure v1 supports OpenAI clients and token refresh. Vertex exposes an
OpenAI-compatible endpoint with Google Cloud authentication. AWS documents both
Chat Completions and native Converse, but API availability, paths, model support
and authorization differ by endpoint. Bedrock's runtime-compatible endpoint also
does not implement OpenAI `GET /models`, so one generic discovery call is not
sufficient.

Sources: [Azure v1 API](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle),
[Vertex OpenAI compatibility](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/openai),
[Google Gen AI SDK](https://googleapis.github.io/python-genai/),
[Bedrock Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html),
[Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html),
[Bedrock structured output](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).

## Necessary gateway extensions

The current gateway is deliberately narrow. Extending it is credible, but these
are real changes rather than configuration-only adapter additions:

Start with separately configured provider-bound gateway instances, retaining
ELSPETH's existing endpoint selection. Today each app constructs one adapter,
token manager and upstream transport
(`gateway/src/elspeth_llm_gateway/core/app.py:242`), and config has one
`upstream_origin` (`gateway/src/elspeth_llm_gateway/core/config.py:126`). This
preserves the current boundary but adds deployment instances to operate. A single
multi-cloud gateway would instead require new core-owned per-route dispatch;
existing model aliases do not already provide it. Do not make that router a
prerequisite for removing LiteLLM.

1. **Core-owned authentication and dispatch.** `InvokePlan` describes a relative
   path, headers and JSON; adapters cannot set `Authorization`
   (`gateway/src/elspeth_llm_gateway/sdk/protocol.py:74`). The transport always
   obtains an OAuth token and writes `Authorization: Bearer ...`
   (`gateway/src/elspeth_llm_gateway/core/transport.py:151`). API keys, AWS
   SigV4/default-chain credentials and Google ADC do not fit that assumption
   unchanged. Add explicit core-owned credential/transport strategies, or an
   equally controlled SDK dispatch boundary. Do not give arbitrary adapters a
   network client and credential access merely to bypass the existing design.
   Azure's dated API also needs a deliberate query-parameter representation;
   `InvokePlan` currently rejects query strings in its path.
2. **Richer typed messages and capability declarations.** The current message
   content is `str | None`; it cannot carry existing image parts or native
   thinking blocks unchanged (`gateway/src/elspeth_llm_gateway/sdk/types.py:58`).
   The capability vocabulary is text/tools/JSON/seed/usage (`:18`). Add only
   required features, with explicit per-route enforcement. Current requests
   reject unknown fields, including token streaming
   (`gateway/src/elspeth_llm_gateway/core/contract.py:194`). Composer event
   streaming must not be confused with provider-token streaming.
3. **Strict tool semantics.** `ChatFunctionDef` has no `strict` field and forbids
   extras (`gateway/src/elspeth_llm_gateway/core/contract.py:49`). Response-format
   JSON-schema strictness does exist (`:159`), but it is a different feature.
   Preserve the distinction between forwarding a strict flag and proving the
   selected model actually enforces it.
4. **Native continuation and accounting.** Preserve opaque signed reasoning and
   Gemini thought signatures without converting them into ordinary text. Carry
   cache-read/write, reasoning, partial/unknown usage, upstream request/model
   identities and cost provenance; current `CanonicalUsage` has only
   prompt/completion/total tokens
   (`gateway/src/elspeth_llm_gateway/sdk/types.py:105`). A provider-specific
   extension must be typed and validated, not an unrestricted passthrough bag.
5. **Attempt evidence across the service boundary.** The transport currently
   replays once after a 401 (`gateway/src/elspeth_llm_gateway/core/transport.py:188`).
   Decide how each physical upstream send is represented under the outer
   logical request. Preserve ELSPETH's attempt accounting without inventing an
   opaque retry loop. Gateway correlation and translated-request identity must
   remain distinguishable from the caller's logical request hash.

Cloud SDKs remain useful implementation dependencies. Their retries and automatic
tool execution must not assume authority over ELSPETH's loop. Google's SDK, for
example, documents automatic function calling and its disable option. A sync
SDK running in a thread also cannot promise that task cancellation stopped the
remote request; unknown completion and settlement require explicit handling.

## Complete LiteLLM removal is broader than routing

Composer's physical transport is a narrow seam, but multiple call roles use it:
planner, loop, Advisor and session auto-title. Preserve the existing prompt guard,
credential scan, durable admission and terminal audit around the replacement.
`src/elspeth/web/composer/provider_quota.py:222` admits attempts, `:263` enforces
SDK entry custody, and `:290` refuses settlement without terminal audit.

Replace LiteLLM-specific exception and provider-resolution logic too
(`src/elspeth/web/composer/provider_errors.py:121`,
`src/elspeth/web/composer/strict_transport.py:124`). Do not keep compatibility
masquerades as the long-term domain model. The existing cache-counter
deduplication workaround needs reinspection against native responses rather
than automatic preservation (`src/elspeth/web/composer/llm_response_parsing.py:123`).

Pricing and discovery are independent removal tasks:

- `src/elspeth/core/llm_pricing.py:414` calculates missing cost through LiteLLM's
  catalog/calculator. This includes cache and tier semantics; a single input and
  output rate is not equivalent. Prefer provider-reported cost where valid,
  otherwise a versioned data-only rate catalog plus an owned calculator. Retain
  explicit custom deployment pricing and unknown cost as unknown, not zero.
  [GenAI Prices](https://github.com/pydantic/genai-prices) is a candidate data
  source, not an approved dependency or proven pricing match. Its current data
  contract must be selected explicitly; the project documents frozen legacy
  snapshots as well as newer versioned data.
- `src/elspeth/plugins/llm/model_catalog.py:101` reads LiteLLM's model inventory.
  OpenRouter has a live startup snapshot with a bundled fallback and records
  catalog provenance. Replace the offline fallback and model-capability data
  deliberately; do not turn validation into an always-online discovery step.

Keeping LiteLLM solely to import its pricing or model list retains executable
package exposure. A completed removal must include these consumers and the
dependency/lock changes, not just route traffic through our gateway.

## Migration and acceptance

1. Freeze the supported route/model/feature matrix for the existing workloads.
   Distinguish parity work from new GCP capability. Bind the implementation to
   the active target revision, including any Composer work that lands later.
2. Qualify a direct OpenAI-compatible Composer call through the existing gateway
   while preserving admission and audit. Separately use a demanding Bedrock
   case with tool replay, signed reasoning and cache accounting to test whether
   the proposed canonical contract is sufficient.
3. Extend gateway auth, content, capabilities and usage only as required by that
   matrix. Keep the existing agency OAuth route supported. Introduce native
   Bedrock and GCP adapters behind the same owned boundaries, selecting concrete
   SDK versions and endpoint/model contracts before qualification.
4. Replace pricing, catalog, exception and routing consumers; remove LiteLLM
   only after the inference and ancillary paths have replacements. Preserve
   existing provider names, deployment identifiers and config meanings where
   feasible. An unavoidable wire/captured-response change needs a declared
   replay boundary, not silent reinterpretation of persisted records.
5. Qualify actual wire behavior and accounting before switching production.
   Do not shadow real prompts to a second provider: that adds cost and data
   disclosure. Use captured/synthetic fixtures first and authorized live canaries
   for provider behavior. Rollback is selection of a previously qualified
   implementation, not automatic provider failover or data downgrade.

Acceptance must cover tools and strict schemas, refusals, output limits,
images, native reasoning continuity, cache hits/writes, absent versus zero
usage, price provenance, credential precedence/refresh, rate limits, 502/503,
timeouts, cancellation, replay without network, redaction, and every physical
attempt's relationship to audit/quota evidence. Reuse the existing gateway
conformance kit, Composer wire-fidelity matrix and affected provider/quota tests.
Run relevant whole-tree gates and required integration checks for the eventual
implementation's actual reach.

## Evidence limits

This evaluation inspected source, dependency declarations/locks, existing test
definitions, the installed OpenAI SDK's Azure/retry interfaces, and current
primary project/vendor documentation. No candidate library was installed, no
test suite or benchmark ran, and no provider, cloud-auth, network fault,
deployment or live acceptance behavior was exercised. Composer's current
sessionless boot-probe function returns `None`; it is not evidence of provider
qualification (`src/elspeth/web/composer/boot_probe.py:253`).

The recommendation rests on existing reuse seams and the bounded provider
scope. It does not establish that replacing LiteLLM is intrinsically safer,
faster or cheaper: vendor SDKs and gateway artifacts still require ordinary
dependency and release hygiene, and the maintenance cost shifts to our supported
adapter contracts. Broader model coverage or a desire to outsource translation
maintenance would strengthen the Pydantic AI or Bifrost alternatives.
