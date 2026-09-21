# Azure Composer and LLM cost failures

This runbook covers the Azure interfaces on `release/0.8.1`.

## Composer cost admission

A successful Azure completion can carry a versioned deployment name that is
absent from LiteLLM's pricing catalog. LiteLLM can then omit its private cost
field even when the original requested model is priceable. Previously the
bounded planner rejected that response with `COST_UNAVAILABLE`, which the UI
rendered as a generic invalid-provider-response error.

Cost admission now uses this order:

1. Preserve a present, non-null `usage.cost` value.
2. If the public field is absent or null, preserve a present, non-null
   `_hidden_params.response_cost` value.
3. Only when neither field supplies a non-null value, call `litellm.cost_per_token` with the
   operator's pricing identity (or the requested model when unset) and
   ELSPETH's validated token counts. Record the
   provenance as `litellm.cost_per_token`.

The third path requires valid reported prompt and completion token counts,
preserves reported cache discounts, cache-write durations, and service tiers,
and rejects malformed counters or impossible cache or reasoning subtotals.
`completion_cost` is deliberately not used: it can prefer the
returned deployment alias even when given the requested model.
It does not estimate tokens from response text, substitute the returned
deployment name, or fabricate zero cost. Boolean, string,
negative, non-finite, or overflowing costs remain unavailable. An unsupported
pricing model or failed pricing calculation also remains unavailable.
The fallback verifies that LiteLLM resolves the model to a real catalog entry
with explicit input and output token prices. A zero returned for an unknown
deployment alias is not pricing evidence: ELSPETH records `provider_cost=null`
and `provider_cost_source="not_available"`. Explicit zero prices in a valid
catalog entry remain valid.
The planner applies its cumulative cost cap to calculated costs exactly as
it does to costs already present in the response.

For the reported production case, a priceable requested model such as
`openai/gpt-5.6-terra` can therefore be priced even when Azure returns an
unpriceable `gpt-5.6-terra-2026-07-09` deployment alias. The model must actually
exist in the running LiteLLM catalog; installing the code does not add prices
for unsupported models.

For a custom routing name such as `openai/gpt-5.6-sol-datazone`, configure
`ELSPETH_WEB__COMPOSER_PRICING_MODEL=azure/gpt-5.6-sol` and, independently,
`ELSPETH_WEB__COMPOSER_ADVISOR_PRICING_MODEL=azure/gpt-5.6-sol` when those
catalogue entries match the actual deployment. The routing name still goes to
the endpoint and remains `model_requested` in audit records. These overrides
do not replace valid provider-reported costs or manufacture missing prices.

If cost admission still fails, inspect the failed call's sanitized audit:

- `model_requested` and `model_returned` distinguish request identity from
  Azure's returned deployment alias.
- `provider_cost_source` distinguishes public, private, calculated, and
  unavailable cost.
- `prompt_tokens` and `completion_tokens` show whether pricing had complete
  reported usage.
- `COST_UNAVAILABLE` indicates accounting failed before proposal parsing or
  tool dispatch. Repeating the request cannot repair a missing catalog entry.

Absent and explicit `null` cost fields both mean that the provider supplied
no price. They permit catalogue calculation, but never fabricate a known price.
Do not log raw prompts,
credentials, or full provider responses to diagnose pricing.

### Clean-process pricing differs from the web worker

ELSPETH defaults `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing
LiteLLM. This deliberately uses the installed package's bundled prices and
prevents an unconfigured pricing-catalog request to GitHub. A standalone
`import litellm` diagnostic can instead download a newer map. A successful
standalone calculation therefore does not prove that the web worker has the
same model entry or prices.

The release dependency now requires LiteLLM 1.102.0 or newer, with 1.102.0
locked for reproducible installation. Its bundled catalog contains the exact
`gpt-5.6-terra` and `azure/gpt-5.6-terra` entries, so this deployment model no
longer requires a remote pricing fetch. Build from the updated lockfile and
remove the deployment's explicit remote-fetch override to use the offline
default. A running container built with the old dependency does not acquire
the new catalog merely by receiving the Python source patch.

Compare the installed LiteLLM version, catalog source, and requested-model
entry in the actual worker and the diagnostic process. Run offline diagnostic
processes with `LITELLM_LOCAL_MODEL_COST_MAP=True` explicitly set to reproduce
the default server policy. For a newer model, use a tested LiteLLM package
whose bundled catalog includes that model. An explicit deployment setting
`LITELLM_LOCAL_MODEL_COST_MAP=False` permits LiteLLM's remote catalog fetch at
startup; it changes the deployment's egress and pricing reproducibility policy.
A failed remote fetch can still fall back to the bundled map. Enabling remote
fetching alone does not guarantee that a new model will be priceable at startup.
Setting it in a separate shell does not replace an already-running worker's
catalog. Do not register guessed prices or substitute a different model to
make the cost cap pass.

## Authentication

The `azure/` Composer provider accepts `AZURE_API_KEY`,
`AZURE_OPENAI_API_KEY`, or `AZURE_AD_TOKEN`. The `azure_ai/` provider instead
uses `AZURE_AI_API_KEY`. These contracts apply independently to the primary
and advisor roles. Explicit endpoint/key configuration retains its existing
behavior. See [environment variables](../reference/environment-variables.md)
for the complete setup.

The pipeline Azure LLM provider uses its configured API key and the OpenAI
SDK directly. It does not use Composer's LiteLLM cost admission. Its config
representation hides the API key; malformed response envelopes retain an
error audit record and telemetry. Azure HTTP 429 is retryable, HTTP 401 is
not made retryable by incidental numbers in its message, and `content_filter`
is classified as a content-policy rejection.

## Execution model and billing identity

LLM transforms and sources accept an optional `pricing_model`. For a
profile-bound pipeline, set it in the operator's profile alongside the routing
binding:

```yaml
llm_profiles:
  sol:
    provider: azure
    model: gpt-5.6-sol-datazone
    deployment_name: gpt-5.6-sol-datazone
    pricing_model: azure/gpt-5.6-sol
    endpoint: https://your-resource.openai.azure.com
    credential_scope: server
    credential_ref: AZURE_OPENAI_API_KEY
```

The equivalent web profile belongs in `ELSPETH_WEB__LLM_PROFILES`. A direct
YAML LLM node can set `pricing_model` in its provider options; an authored node
that selects an operator profile cannot override its private billing identity.

The provider still receives `gpt-5.6-sol-datazone`. Call-response audit payloads
retain the provider-returned model and record `pricing_model`, `provider_cost`,
and `provider_cost_source`. This accounting works without an external tracing
service. Missing pricing stays `null` / `not_available`; it does not fail an
otherwise successful transform. Composer's separate cost-budget refusal remains
in force. Token quotas and provider routing do not change with this setting.

## Azure LLM temperature

Azure source and transform temperature defaults to `null`, which omits the parameter
from the SDK request. This supports reasoning deployments that reject an
explicit `0.0`. Standard models can still use an explicit value, such as
`temperature: 0.7`. The same behavior applies to single-query and multi-query
transforms, including nodes bound through an LLM profile. Explicit values
are preserved; ELSPETH does not infer capabilities from deployment names.

For profile-bound nodes, temperature belongs to the operator's profile, not
the authored pipeline options. An absent profile setting retains the provider
default; explicit `null` omits the wire parameter, and an explicit number is
forwarded. A pipeline cannot override this binding. Keep old operator profile
aliases configured while retained sessions still reference them; changing the
default alias affects new authoring and does not rewrite historical states.

If every row fails, the tutorial run endpoint reports the durable zero-output
outcome with HTTP 409 and the run's row counts. It does not attempt to preview
a nonexistent output artifact. Successful and partially successful runs still
require verified output artifacts.

## Deployment boundary

The dedicated `cost_unavailable` guided-operation failure code changes the
Sessions schema from epoch 63 to 64. The pre-1.0 schema policy requires store
recreation; there is no automatic migration. Do not replace or delete a
production store without the operator's explicit authorization. Local tests
and mocked Azure responses do not establish live production acceptance.
