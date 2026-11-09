# `list_sources` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Discover source plugins that this request is permitted and locally equipped to configure. This supplies the orchestrator's capability search for input acquisition; it does not reserve a plugin or prove that a particular configuration can execute.

Select before choosing a source implementation. Requires a PolicyCatalogView and its identical PluginAvailabilitySnapshot. No blob, secret-service or execution context is required. Structural nodes such as gates and queues are not plugins and are not enumerated by these listings.

## 2. Contract

Arguments are exactly an empty object; extra keys are rejected. The result is a ToolResult with data.available and data.prohibited, plus the current state/version and validation envelope. Available summaries include config_fields, defaults, usage guidance, composer_hints, secret_requirements and declared capabilities. Only the categorical WEB_SURFACE_PROHIBITED reason appears in prohibited. Other unavailable plugins are omitted by design. There is no write, approval requirement, provider call or retry. Successful responses are cacheable only within the compose call; failures are not cached. The policy/plugin set is a request/process snapshot, so this is discovery at that scope, not a live credential-health probe. Repeated calls are observationally idempotent.

This is discovery: the handler retains immutable composition state; successful declared inventory data is admitted under its producer response contract before audit/disclosure. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:172](../../../../src/elspeth/web/composer/tools/sources.py#L172) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/catalog/policy_view.py:96](../../../../src/elspeth/web/catalog/policy_view.py#L96) — dependency, contract or downstream consumer.
- [src/elspeth/web/catalog/service.py:204](../../../../src/elspeth/web/catalog/service.py#L204) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/inventory_response_contracts.py:1](../../../../src/elspeth/web/composer/inventory_response_contracts.py#L1) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/discovery_cache.py:80](../../../../src/elspeth/web/composer/discovery_cache.py#L80) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Normal operation projects available entries from plugin-owned declarations through policy and operator-profile views. Empty available is a valid discovery result, not an error. An absent SDK, missing credential, unavailable profile or unapproved plugin can remove an entry without placing it in prohibited. Invalid arguments are rejected before requested access; a fault in an admitted catalog/schema remains an owned-code failure and must not be disguised as an unknown plugin. Cache hits reconstruct the current state envelope and re-admit the cached producer response. MCP uses a trained-operator policy view, so a name visible there need not be selectable in an authenticated web request.

## 5. Usability assessment

**SG-05 — intended disclosure restriction with a planning limitation; high confidence.** The orchestrator sees selectable plugins and categorical bans, but not every temporarily unavailable capability or its repairable prerequisite. It cannot conclude from an absent name whether installation, credentials, a profile or authorisation is missing. The policy view documents this deliberate choice; it is not evidence of a registry defect. Workaround: a named selection attempt or operator inspection supplies a denial reason. Lists are sufficient for ordinary flat options, but nested enums/unions still require get_plugin_schema. No behavioural defect in this handler was demonstrated by the focused checks.

## 6. Structural remediation

Keep authoritative configuration and capability declarations on source plugins, policy visibility in the policy layer, and capability selection in the orchestrator. If capability planning needs repairable absences, add a deliberately bounded availability query/report rather than bypassing policy or making each list disclose private bindings. All three inventory tools, schema/assistance lookup and mutation failures must share the same snapshot and closed reason vocabulary. Acceptance: a selectable plugin appears with plugin-owned defaults; a banned plugin appears only under prohibited; a missing local prerequisite is reported through the authorised availability surface without exposing secret values; cache reuse cannot transplant another principal's snapshot. Compatibility: preserve available/prohibited and make additional availability evidence explicitly versioned or additive. Implementation risk medium because disclosure and cache scope are security boundaries.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_discovery.py:43](../../../../tests/unit/web/composer/test_tools_discovery.py#L43) — TestDiscoveryTools list delegation, snapshot visibility, unavailable-plugin denial and schema-fault tests.
- [tests/unit/web/composer/test_tools_discovery.py:299](../../../../tests/unit/web/composer/test_tools_discovery.py#L299) — test_discovery_denial_precedes_requested_access_and_preserves_saved_findings.
- [tests/unit/web/composer/test_tools_discovery.py:748](../../../../tests/unit/web/composer/test_tools_discovery.py#L748) — test_cacheable_discovery_is_opt_in_with_named_mutable_complement.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.sources._handle_list_sources`](../../../../src/elspeth/web/composer/tools/sources.py#L172).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```
