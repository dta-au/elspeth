# Design: `azure_search_retrieval` plugin and operator profiles

Status: draft for review. Branch `feature/azure-ai-search-rag` (off
`release/0.8.1`). Target release: 0.8.1 unless ruled otherwise.

## Goal

A web-authored pipeline can retrieve context from an operator-configured Azure
AI Search service, and one install can offer several such services, without a
web author ever choosing the endpoint a server credential is sent to.

## What is true today (measured on `8d98d2946`)

- `rag_retrieval` has two providers, `chroma` and `azure_search`, selected by
  `provider` with an untyped `provider_config: dict`. The plugin's JSON schema
  therefore shows the composer's planner none of the Azure fields.
- The web layer refuses `provider_config.use_managed_identity` at six call
  sites in three layers (four composer tools, `validate_managed_identity_policy`,
  the execution service). Both non-tool checks run on the **profile-lowered**
  state. The refusal message names an operator allowlist that does not exist.
- The Azure Container Apps bundle has no Key Vault slot for a plugin API key,
  so on that target there is no working credential path from a web session.
- Operator profiles (`plugin_policy/profiles.py`) are per plugin: a registered
  resolver replaces the plugin's whole public schema with a `profile`-required
  one, and `plugin_policy/validation.py:470` turns a node without a valid alias
  into `profile_unavailable`. Registered on `rag_retrieval` that would make
  Chroma profile-only, which breaks the web flow where a session indexes a
  Chroma collection under its own data directory and then queries it.
- Rate limiting is keyed `get_limiter("azure_search")`: every Azure Search node
  shares one bucket regardless of which service it targets.

## Decisions already made (John, 2026-09-20)

1. Mandatory operator profile for web-authored Azure Search; a web author never
   types an endpoint.
2. A dedicated plugin, not an extension of the profile machinery.
3. Several RAG sources per install must be supported.
4. Pre-release, no tech debt: one path to Azure Search, so `azure_search`
   leaves `rag_retrieval`.
5. Pins and signatures never shape the code; signature churn is the operator's
   signing obligation.

## Design

### 1. Shared retrieval core

`RAGRetrievalTransform` today mixes provider-neutral work (query build, search
call, zero-result handling, context formatting, the `__rag_*` output fields,
Welford score telemetry) with provider selection. Split it:

- `plugins/transforms/rag/core.py`: `RetrievalTransformBase(BaseTransform)`, no
  `name`, holding everything provider-neutral. Two hooks:
  `_build_provider(ctx) -> RetrievalProvider` and `_collection_name() -> str`.
  `RetrievalOutputConfig` carries the shared options (`output_prefix`,
  `query_field`, `query_template`, `query_pattern`, `top_k`, `min_score`,
  `on_no_results`, `context_format`, `context_separator`,
  `max_context_length`) with their existing validators.
- `rag_retrieval` keeps `provider` / `provider_config` with `chroma` as the
  only registry member. Its behaviour does not change.
- Output fields stay `{prefix}__rag_context|score|count|sources` for both
  plugins, so an `llm` prompt template works with either.

This is a behaviour-preserving move for Chroma; the existing
`tests/unit/plugins/transforms/rag/` suite is the regression net and must pass
unmodified except for the Azure cases that move.

### 2. `azure_search_retrieval`

`plugins/transforms/azure/search_retrieval.py`, discovered through the existing
`transforms/azure` directory.

- Typed, top-level options: the shared retrieval options plus `endpoint`,
  `index`, `api_key`, `use_managed_identity`, `client_id`, `api_version`,
  `search_mode`, `vector_field`, `semantic_config`, `content_field`,
  `id_field`, `title_field`, `url_field`, `select`, `filter`,
  `request_timeout`. The config model composes the existing
  `AzureSearchProviderConfig` validators rather than restating them; the
  provider class in `clients/retrieval/azure_search.py` is reused as is.
- `determinism = EXTERNAL_CALL`, `content_trust = UNTRUSTED`,
  `passes_through_input = True`, capability tags `rag`, `retrieval`,
  `vector-search`, `azure`.
- **Readiness uses the platform facility.** `requires_runtime_preflight = True`
  and `runtime_preflight(ctx)` runs the index count probe, so the check is an
  audited, retried `runtime_preflight` operation. The plugin never calls
  `ctx.record_readiness_check`. `on_start` only builds the provider (it runs
  before the preflight, in `run_context_factory`).
- Removed from `rag_retrieval`: the `azure_search` registry entry, the
  `RetrievalProviderName` member, the `_configured_collection_name` arm, and the
  Azure `example_use` / `probe_config`, which become Chroma-shaped.

### 3. Operator profiles

```bash
ELSPETH_WEB__AZURE_SEARCH_PROFILES='[
  {"alias": "policies", "endpoint": "https://a.search.windows.net",
   "auth": "managed_identity", "client_id": "<client id>",
   "indexes": ["approved-documents"]},
  {"alias": "contracts", "endpoint": "https://b.search.windows.net",
   "auth": "api_key", "credential_ref": "SEARCH_B_KEY"}
]'
```

- `AzureSearchProfileSettings`: `alias`, `endpoint` (HTTPS, and for
  `managed_identity` a `.search.windows.net` host, the provider's existing
  rule), `auth`, `client_id` (managed identity only), `credential_ref` (api key
  only; server-scoped, so the name must be in
  `ELSPETH_WEB__SERVER_SECRET_ALLOWLIST`, the LLM profile precedent),
  `indexes` (optional pin; absent means any index on that service),
  `api_version` (optional).
- `_AzureSearchProfileResolver` on `PluginId("transform",
  "azure_search_retrieval")`:
  - public schema: `profile` (enum of usable aliases) plus the author options.
    Private binding names are `endpoint`, `api_key`, `use_managed_identity`,
    `client_id`, `api_version`; an authored node carrying any of them fails
    lowering with `private_profile_option`.
  - lowering injects the binding; with `indexes` set, an `index` outside the pin
    is refused.
  - availability: a `managed_identity` profile is usable when `azure-identity`
    is importable; an `api_key` profile when the credential resolves in the
    inventory.
  - `selected_alias`: the only usable alias when there is exactly one, else
    none (the Textract rule; no silent default among several sources).
- `azure_search_retrieval` is unavailable to web sessions when no profile is
  configured, the `aws_s3` source posture.
- The rate limiter is keyed `azure_search:<alias>` for profiled nodes (the
  endpoint host for YAML runs), so two services do not share a bucket.
- Audit: `audit_safe_options` records `profile: <alias>`. No new
  `Profiled*AuditIdentity` type: the endpoint is already in every recorded
  search call, unlike a Textract bucket, so the alias adds the only missing
  fact. (Bedrock guardrail profiles set the same precedent.)

### 4. The managed-identity refusal

`web_rag_provider_config_policy_error` and its six call sites are deleted with
the `azure_search` provider they guarded: once `rag_retrieval` cannot reach
Azure, the check has nothing to fire on. The guarantee moves to the profile
machinery, which is structural rather than a string match: a web-authored
`azure_search_retrieval` node without a valid alias never lowers, and one with
a private option is refused. `CHECK_MANAGED_IDENTITY_POLICY` and its validation
phase go too.

This is the one step to review hardest. Proof obligations before it lands:

- a test per layer (composer tool, `validate_pipeline`, execution service)
  showing an authored node that names `endpoint`, `use_managed_identity` or
  `client_id` is refused, with a mutant that lets the option through going red;
- a test that a node with no `profile`, and one with an unknown alias, never
  reaches provider construction;
- what the trained-operator (local MCP) snapshot admits for this plugin is
  measured, not assumed: `is_trained_operator` short-circuits several profile
  checks today, and the plan must state whether raw options are reachable there.

### 5. Parity surfaces

From `grep -rl aws_textract_profiles` and `grep -rl rag_retrieval`:

- `web/config.py`: the settings field, its validator, `_JSON_ARRAY_FIELDS`, the
  safe-error-path set.
- `plugin_policy/profiles.py`: `RuntimeWebPluginConfig`, the registry arm, the
  exact-type `public_summary` arm (usage text, example), the assistance arm.
- `web/audit_readiness/boundary_expectations.py` and `explain.py`,
  `web/execution/preflight.py` (the nested `persist_directory` walk stays
  Chroma-only), `contracts/errors.py`.
- frontend `pluginDisplayName` and its test.
- goldens and pins: `tests/golden/web/catalog/knob_schema/` (new file, changed
  `transform__rag_retrieval.json`), `fingerprint_baseline.json`,
  `test_discovery.py`, `test_external_catalogue_metadata.py`,
  `test_state_engine_config_discriminators.py`, `test_plugin_wiring.py`,
  `core/rate_limit/test_registry.py`, the canonical hash corpus.
- composer teaching text for the new plugin; `examples/azure_search_rag`
  switches to the new plugin.
- docs: the ACA cold-install section 10 gains the
  `ELSPETH_WEB__AZURE_SEARCH_PROFILES` entry through `extraEnvironment` (it is
  not a secret) and loses its "cannot be used yet" limit;
  `docs/reference/environment-variables.md`; `CHANGELOG.md`.

## Interaction with the Document Intelligence plan

Task 7 of `2026-09-20-azure-document-intelligence-capability-expansion.md`
moves RAG onto `runtime_preflight`, deletes `record_readiness_check`, and gives
`RetrievalProvider.check_readiness` `operation_id` / `coordination_token`
keywords for both implementers in one commit. This design does the Azure half
of that early. Whichever lands second rebases; Task 7 shrinks to Chroma plus
the verb and CHECK-value deletion. The shared core must leave Chroma's
`on_start` readiness path untouched so the two do not fight over it.

## Out of scope

- A Search private endpoint (SSRF validation refuses private addresses).
- An API-key Key Vault slot in the ACA bundle; managed identity is the ACA path.
- Row-templated OData filters, agentic retrieval / knowledge bases, a
  start-up readiness probe per profile.

## Open questions for John

1. `indexes` absent means "any index on the service". Acceptable, or should the
   pin be mandatory?
2. Do the Azure half of Task 7 here (audited probe, `runtime_preflight`), or
   wait for Task 7 and start on the `on_start` path?
3. Plugin name: `azure_search_retrieval` (matches `rag_retrieval`) or
   `azure_ai_search` (matches the product)?

## Verification

Test-first throughout. Affected suites plus every whole-tree gate whose inputs
change (discovery, goldens, fingerprint baseline, hash corpus, architecture).
Plugin registration and web policy are shared runtime behaviour, so the full
`pytest tests/` gate (`scripts/full-suite-gate.sh --execute --detach`) runs
before merge. No schema or SQL changes, so no testcontainer run. No live Azure
test without an operator-supplied service.
