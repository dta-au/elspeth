# `validate_secret_ref` — tool reference and intended contract

Check one named secret reference for the authenticated user's current resolvability. The result is metadata: `available=false` is a successful answer to the check. Listed references carry inventory metadata with availability refreshed by the live resolver; unlisted references carry only their name and availability.

| Version and scope | Value |
|---|---|
| Tool identity | `composer.validate_secret_ref`; wire name `validate_secret_ref` |
| Intended contract | 1.0 candidate documentation of existing promises; no new semantics adopted |
| Implementation reviewed | Commit `9c22be98e4323465c3472188a034f893cfec983e`; tree `086dd40185fd6808d86092f353421f7dde592301`. Release association not established. |
| Carried to 0.8.10 | Carried from donor `60119def4` (`codex/tool011-validate-secret-ref-20261021`) onto `release/0.8.10-assembly`, where U-30 has retired the pipeline-planner route. Statements about that route are corrected below; the binding companion is carried unchanged and still describes the audited base (see [Version maintenance](#version-maintenance-and-evidence-limits)). |
| Surfaces | Web freeform Composer through the common compose loop (`composer_loop_tool_definitions`); direct synchronous dispatch. CLI Composer MCP excludes secret tools. |
| Evidence | Independent source/document accuracy and calling fitness passed after the same reviewer's focused `VSR-R1` correction check; source-scoped tool purpose rated OK. The companion binds the reviewed artifacts and report. Source review plus narrowly executed synthetic conformance controls; no whole-tool/runtime/deployment approval. |
| Material caveat | Readiness is point-in-time. Inventory and readiness are separate reads; a listed failure reason can describe the earlier inventory snapshot. Unlisted results have no `reason`. |

[Call guide](#call-guide) · [Availability](#availability-authority-and-supplied-instructions) · [Intended contract](#normative-intended-contract) · [Inputs](#current-input-contract-and-transport) · [Outputs](#current-outputs-refusals-and-caller-recovery) · [Implementation](#current-white-box-implementation) · [Lifecycle](#current-state-custody-and-lifecycle) · [Assessment](#formal-source-audit-agreement-known-nonconformance-and-open-questions) · [Verification](#verification-and-adversarial-controls) · [Maintenance](#version-maintenance-and-evidence-limits).

## Call guide

1. Read the complete `list_secret_refs` result before describing credential state. Choose the reference intended for the user's requested provider or plugin; preserve the exact name.
2. Call `validate_secret_ref` with `{"name":"SELECTED_REF"}` using the currently offered tool. User identity, auth provider and secret service come from the server context; they are not arguments.
3. Read `data.available`, independently of outer `success` and `validation.is_valid`. `success=true, available=false` means the check completed and the reference did not resolve. `validation` describes the unchanged composition.
4. If listed and unavailable, report the returned structural `reason`. If unlisted, report that this check did not establish availability; do not invent a scope or reason. Operator configuration, a missing reference or a corrupt stored row require their respective repair, not invented credential material.
5. Use `wire_secret_ref` only when appropriate to the user's request and when validation reports availability. Its destination allowlist is a separate gate; validation cannot grant that authority. Execution still resolves later and requires its own approval/admission.

Names are case-sensitive exact strings at this interface. The tool does not normalize, trim, uppercase or impose a name grammar. That does not imply an empty, spaced, Unicode or oversized name can be created in a backing store.

## Availability, authority and supplied instructions

| Surface / role / condition | Offering and authority | Definition / instructions | Evidence and limit |
|---|---|---|---|
| Flat registry | Registered as `SECRET_DISCOVERY`; no graph effect; not cacheable. | `_VALIDATE_SECRET_REF_DECLARATION`; source schema below. | Current source and builder capture; registration alone is not a delivered request. |
| Web freeform Composer | `composer_loop_tool_definitions(dialect)` includes the entry regardless of whether secret context is usable; invocation without service/user refuses. Authenticated user and scoped resolver are supplied outside model arguments. | `none` or `openai_strict`; static capability core plus interaction skill and possible deployment overlay. | Both local builder variants executed. Effective deployed model/config, overlay and final adapter delivery remain `UNVERIFIED`. |
| Pipeline planner (retired) | At the audited base a separate planner builder offered the same entry. U-30 retired that route on this branch; `PLANNER_DISCOVERY_TOOL_NAMES` and `planner_tool_definitions` no longer exist in `src/`. | Not applicable. | No second builder remains, so the base's builder-equality control was dropped on carry (see `VS-C5-BUILDERS`). |
| Advisor checkpoint | Inspected request path uses `tools=None`; Composer's ability to request advice does not offer this tool to Advisor. | Advisor prompt/context are a different role. | `advisor_checkpoint.py` provider call; source-only, no remote request capture. |
| CLI Composer MCP | Excluded: `_COMPOSER_TOOL_NAMES` is core discovery plus mutation registries, not secret registries. | No invented MCP alias or CLI credential context. | `composer_mcp/server.py:116`; source-only. Other deployment wrappers are not inventoried here. |
| Direct Python handler / dispatch | `ToolContext.secret_service` and `user_id` must exist. Production uses `ScopedSecretResolver`, binding auth provider to service operations. | Direct handler context check precedes Pydantic admission; public schema admission precedes context refusal. | Tested only at specified synchronous seams; authenticated HTTP/session admission not executed. |

### Exact declaration and consequential instruction text

The declaration description at [tools/secrets.py:271](../../../src/elspeth/web/composer/tools/secrets.py#L271) is:

> Check if a secret reference exists and is accessible to the current user. Returns the checked reference `name` and `available` (true when it resolves for you). When the reference appears in your inventory, also returns its `scope`, `source_kind`, and `reason` (null when available; otherwise the reason it cannot resolve).

This is repository-authored text, reproduced in full. The exact argument description is in the JSON schema below.

The [capability core, “Canonical proposal and discovery”](../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#canonical-proposal-and-discovery) says:

> Model identifiers come from the supplied model catalog where the request provides one, and otherwise only from `list_models`. Read the complete `list_secret_refs` result before describing credential state, validate the selected reference, and use the discovered schema's secret-reference object; never invent an identifier or substitute a raw value, `secret://` URI, or environment interpolation.

Its preceding instructions require missing-fact discovery, reject repeated/supplied reads on proposal-only requests, and require discovery before the terminal proposal. They do not grant runtime authority or allow credential values in authoring discovery. The interaction skill's “Tool Inventory” lists the three secret tools; its “Skill Router” table directs secret questions to discovery results.

Conditional pre-finalisation guidance in [`composition_completion.py:838`](../../../src/elspeth/web/composer/composition_completion.py#L838), for `fabricated_secret`/`missing_secret_ref`, requires `list_secret_refs` and `validate_secret_ref` before asserting availability, asks for the returned reason, forbids literal credentials/interpolation/placeholders, and says: “Only wire {secret_ref: NAME} after validate_secret_ref reports available=true.” For `unauthorized_secret_ref`, separate guidance explicitly says revalidation cannot authorize the destination.

`prompts.render_system_prompt` prepends the exact capability core to the interaction skill after comment/fallback stripping, then appends a deployment skill when configured. Stateful Composer retains its rendering at construction; stateless helpers cache it. At the audited base the planner also assembled request-owned context and information manifests; that route is retired on this branch. Exact shipped passages, heading ancestry, text digests and assembly dependencies are in the [binding companion](validate_secret_ref.binding.json); deployment overlay bytes, final messages and role-selected request state were not captured. The conditional guidance asks for reasons generally, while the declaration correctly limits reason presence to listed references; callers must preserve that distinction.

## NORMATIVE intended contract

These clauses restate existing declaration/guidance and project integrity requirements. The version identifies this interpretation, not adoption of new product behavior.

| Clause / stable ID | Intended requirement | Authority | Current conformance status |
|---|---|---|---|
| C1 / `VS-C1` | Accept one required non-null string `name`, reject undeclared keys and invalid types; preserve exact reference identity. | Declared closed schema and `_ValidateSecretRefArgumentsModel`. | `SOURCE_REVIEWED_ONLY` overall; `VS-C1-SHAPE` is `TESTED_PASS` for the explicit synchronous and local-dialect cases below. |
| C2 / `VS-C2` | Evaluate access for the server-supplied current user; missing secret-service/user context is an explicit refusal, not a missing-reference answer. | Declaration “current user”; context guards; auth-provider-scoped resolver contract. | `SOURCE_REVIEWED_ONLY` overall; `VS-C2-CONTEXT` is `TESTED_PASS` for missing-context synchronous controls. HTTP/auth-provider and cross-session delivery remain unexecuted here. |
| C3 / `VS-C3` | Report checked `name` and live `available`; include `scope`, `source_kind`, `reason` only for an inventory match. Clear reason on availability; preserve supported failure taxonomy otherwise. | Exact declaration; `SecretInventoryItem` invariant. | `SOURCE_REVIEWED_ONLY` universal claim; `VS-C3-STATUS` is `TESTED_PASS` for the explicit listed/unlisted, reason and serializer cases. Atomic diagnosis across concurrent reads is `UNVERIFIED`. |
| C4 / `VS-C4` | Return reference metadata, never secret values; malformed selected producer data must fail rather than disclose undeclared fields. | Capability core; selected-response admission and integrity policy. | `SOURCE_REVIEWED_ONLY` overall; `VS-C4-EGRESS` is `TESTED_PASS` for selected-response negative/positive controls. No generic secret-detector or live-payload qualification. |
| C5 / `VS-C5` | Supply the tool with the declared schema on the offered role/dialect and enforce the semantic schema at dispatch. | Declaration, wire projection and the common compose-loop builder. | `SOURCE_REVIEWED_ONLY`; `VS-C5-BUILDERS` is `TESTED_PASS` for the local compose-loop builder in both dialects; final delivery remains `UNVERIFIED`. |
| C6 / `VS-C6` | Discovery preserves composition state and does not create wiring or execution approval. A later use must retain its own validation/authorization gates. | Discovery effect declaration, capability core, AGENTS secret-wiring and Composer invariants. | `SOURCE_REVIEWED_ONLY` universal claim; `VS-C6-READ` is `TESTED_PASS` for unchanged state, no declared graph effects and repeated synchronous readiness changes. |
| C7 / `VS-C7` | Distinguish malformed calls, unavailable references, context refusal and internal faults; no unexpected store fault becomes a false successful miss. | Typed argument boundary; integrity and explicit-failure policy. | `SOURCE_REVIEWED_ONLY`; `VS-C7-FAULT` is `TESTED_PASS` only for list/has synchronous RuntimeError propagation and preserved graph. Async conversion, cancellation and persistence are source-reviewed only. |

Backwards compatibility: this audit changes no public interface, configuration, persistence or operator workflow. The two existing success data shapes are retained. Scope selection, stronger diagnosis or mandatory listing are not new runtime requirements adopted by this page.

## CURRENT input contract and transport

### Semantic arguments

```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "Secret reference name (e.g. 'OPENROUTER_API_KEY')."
    }
  },
  "required": ["name"],
  "additionalProperties": false
}
```

| Argument | Meaning / type | Omission / default / null | Constraints / coercion / boundary |
|---|---|---|---|
| `name` | Exact reference label, string | Required, no default; omission/null reject. | No declared min/max length, regex, unit or normalization. JSON number/bool/array/object reject before service access. Direct Pydantic construction can accept Python-specific string inputs, such as bytes, that are not the public JSON surface; no Python-helper coercion is a transport promise. |
| Other keys | None admitted | `scope`, `user_id`, provider and target are not options. | Closed semantic root and Pydantic `extra="forbid"`; no authority can be supplied through extra keys. |

The backing-store creation grammar is a separate contract. A string admitted here is a query, not proof that the name can be stored or exposed. Empty, whitespace, Unicode and 4,097-character synthetic names reach a synthetic resolver unchanged; this measures neither a production maximum nor real-store acceptance.

### Offered wire variants and decode

| Variant / role | Exact delta from semantic declaration | Required / null | Decode and evidence |
|---|---|---|---|
| Flat registry | `{name,description,parameters}` from declaration. | `name` required string. | Locally captured current builder. |
| `none`, compose loop | `{"type":"function","function":<flat declaration>}`; no `strict` key. | Same schema; no envelope or optional/null promotion. | Decode retains the JSON object. |
| `openai_strict`, compose loop | Same wrapper and parameters, plus `function.strict=true`. | Same one required string; null remains invalid, not omission. | No promoted path, null-strip, marker or tool-specific envelope. `wire_conformant` is classification; semantic validation still decides admission. |

At the audited base the freeform and planner entries compared equal in both dialects; on this branch the compose loop is the only builder, and its entry is the wire projection's entry unchanged. The exact full definitions at the audited base, including descriptions, producer symbols and roles, are retained in the binding companion. No claim is made that a provider obeys `strict`, that every request offers default policy, or that unknown deployment aliases do not exist. The only audited dispatch name is `validate_secret_ref`; `composer.validate_secret_ref` is this documentation's qualified identity.

## CURRENT outputs, refusals and caller recovery

### Result envelope and success variants

Normal selected serialization supplies `success`, `validation`, `affected_nodes`, `version`, and success `data`. `validation` contains `is_valid`, `errors`, `warnings`, `suggestions`, `semantic_contracts`, `graph_repair_suggestions` according to [`ToolResult.to_dict`](../../../src/elspeth/web/composer/tools/_common.py#L1079). Discovery keeps `affected_nodes=[]` and the incoming state/version. Validation is normalized against the request's policy catalog; successful metadata discovery does not assert a valid graph. No mutation echo, validation delta, preview receipt or execution approval is produced by this handler. Shared optional envelope fields are not unconditional tool guarantees.

| Condition | Complete `data` fields | Meaning / subsequent boundary |
|---|---|---|
| Exact inventory name match | `name:string`, `scope:"user"|"server"|"org"`, `source_kind:string`, `available:boolean`, `reason:null|closed reason` | Scope/source_kind come from that inventory read; availability is overwritten by the subsequent `has_ref`. Production service lists user/server stores; `org` is an admitted contract literal, not evidence of an implemented org store. |
| No inventory match | `name:string`, `available:boolean` | Absence from this inventory snapshot is not forced to false; live readiness can independently return true. No scope, source_kind, reason or fabricated not-found error. |
| Missing context / failed schema ToolResult | No `data` | Explicit failure; not an availability answer. |

Closed reasons are `fingerprint_resolver_not_configured`, `env_var_not_set`, `value_decryption_failed`. Listed result invariant: `available` iff `reason is null`. On live success the handler clears any earlier reason. On live failure it preserves a non-null inventory reason first; otherwise `source_kind=="env"` or `scope=="server"` maps to `env_var_not_set`, and other items map to `value_decryption_failed`. These are diagnostic selection rules, not a transactionally isolated root-cause proof. `source_kind` is an open string; current built-in stores use `user_store` and `env`.

Selected admission accepts the exact five-field inventory form or exact two-field unlisted form, nominal owned records or admitted mappings. It requires exact string/bool scalar types, admitted scope/reason and inventory coherence, reconstructs immutable owned metadata, and binds a precise encoder. Extra fields, including a synthetic `value`, cannot pass that admission. There is no secret-specific compact projection: the compose loop's tool message for an admitted discovery result is `serialize_admitted_discovery_result` ([`tool_batch.py:3894`](../../../src/elspeth/web/composer/tool_batch.py#L3894)).

### Ordered refusals, faults and recovery

| Stage / trigger | Observable outcome and code/message | Effect and recovery |
|---|---|---|
| General `execute_tool` credential-material or catalog/snapshot guard | Credential boundary exception or `ValueError("plugin_snapshot_catalog_mismatch")`, before handler. | Shared guards; no readiness result. Repair context/data through its owner; do not weaken the guards. Credential detection itself not executed by this unit. |
| Public closed-schema failure | Read-only seam / `raise_schema_argument_errors=True`: `ToolArgumentError`; schema-shape category for required/type/extra failures. General `execute_tool(validate_arguments=True)` default returns failed ToolResult with `Invalid arguments for tool 'validate_secret_ref': <schema summary>.` | No list/has access. Correct to declared arguments. Compose loop may serialize an ARG_ERROR rather than this returned failure. |
| Missing secret service or user after public shape admission | `success=false`; `secret_context_unavailable`; `Secret tools require secret service context.` | Same saved graph, no secret reads, no data, no discovery mutation guidance. Context must be supplied by server wiring. |
| Direct handler only: bad model arguments with usable context | `ToolArgumentError`, category `MODEL_VALIDATION`; argument `validate_secret_ref arguments`, expected `object conforming to _ValidateSecretRefArgumentsModel`, actual type `ValidationError`. | Context check occurs first at this private seam. Public dispatch does not inherit that ordering. |
| `list_refs` fault | Unexpected exception propagates from synchronous seam; no `has_ref`. | No empty/missing-success fallback. Unit tested injected RuntimeError; storage-specific failure classes and async HTTP mapping not executed. |
| Live `has_ref` false | `success=true`, `available=false`; listed reason as above or two-field unlisted result. | No graph mutation; report evidence. The call does not create a missing credential, authorize a destination, or grant approval. |
| `has_ref` fault | Unexpected exception propagates after inventory read. | Earlier read work may have happened. Do not interpret absent result as availability false or no work. |
| Malformed selected success payload | `FrameworkBugError("Malformed secret inventory response")` or `FrameworkBugError("Malformed secret reference response")`, depending on failed arm. | Integrity failure, not recoverable absence. Negative egress fixtures exercised; do not retry blindly. |
| Enclosing async batch/provider/audit failure | Different request-level error/disposition; a sync return is not final HTTP or persistence success. | Preserve earlier/sibling effects and audit obligations. Source-only trace below; no rollback claim. |

### Limits and measurements

There is no tool-local string length, inventory-count, byte or time limit in this declaration/handler. Inventory lookup scans until an exact match and is followed by one targeted live probe; this is not bulk decryption. Common request, prompt guard, provider, storage and timeout limits still apply. Audit response projection separately bounds depth 32, container width 64, nodes 1,024 and encoded output 65,536 bytes (`redaction.py:142`); these do not define a safe input maximum or guarantee full persistence of arbitrarily long names. Limit-edge behavior at those common bounds is `UNVERIFIED` here.

### Worked call and result examples

These are synthetic **data fragments**, not full state-validation envelopes or claimed live provider calls. The r005 tests exercise equivalent listed/unlisted status and serializer behavior.

**Listed user row, live decryption failure** — choose a listed `SELECTED_REF`:

```json
{"name":"SELECTED_REF"}
```

```json
{"name":"SELECTED_REF","scope":"user","source_kind":"user_store","available":false,"reason":"value_decryption_failed"}
```

Outer operation success remains true. This is evidence for the current probe, not permission to substitute the server's same-name credential. A stored user row controls shadowing even when undecryptable.

**Unlisted reference** — the standard no-match snapshot with live false:

```json
{"name":"UNKNOWN_REF"}
```

```json
{"name":"UNKNOWN_REF","available":false}
```

Do not expect `reason`; do not claim a specific deployment fault. A concurrent change can also yield an unlisted `available=true` answer.

**Invalid selector extension** — `{"name":"SELECTED_REF","scope":"server"}` is rejected by public schema admission before any store read. There is no scope override for bypassing a user shadow. Null and omitted `name` fail on both dialects.

## CURRENT white-box implementation

### Control and data flow

| Stage / owning symbol | Trust and ordered work | Destination / failure | Evidence |
|---|---|---|---|
| Declaration → `_registry` → wire builder | Server-owned declaration selects handler, `SECRET_DISCOVERY`, no effects, noncacheable response contract. The compose-loop builder projects the requested dialect. | Model-facing schema; request preparation is distinct from provider delivery. | `tools/secrets.py:271`, `_registry.py`, `wire_projection.py`, `provider_gateway.py:431`. |
| Decode → public dispatch | Untrusted JSON decoded under offered W, then semantic closed-schema admission. The compose loop explicitly thaws decoded/frozen arguments before dispatch; a raw Python MappingProxy is not a JSON object admitted by jsonschema. | Typed argument failure, then context guard. | `wire_projection.decode_wire_arguments`, `tool_batch.py:1650`, `_dispatch.py`. |
| `_handle_validate_secret_ref` | Server context service/user checked, local Pydantic model checked, exact `name` extracted. | Find first exact inventory match from `list_refs(user_id)`; all inventory can be read but no per-row handler decrypt. | `tools/secrets.py:234`. |
| `ScopedSecretResolver.list_refs/has_ref` | Binds configured auth-provider type to calls; caller cannot supply it here. | `WebSecretService`; this adapter supplies scoping, not an independently captured authenticated request. | `secrets/service.py:382`. |
| `WebSecretService.list_refs` | User inventory, unless user secrets disabled, merges over server inventory by name and sorts. | Metadata snapshot. | `secrets/service.py:169`; `user_store.list_secrets` selects names only. |
| `WebSecretService.has_ref` | If a user record exists, its bounded `has_secret` result controls; otherwise server has_secret. Disabled user scope never shadows. | Readiness boolean; no fallback through an undecryptable user row. | `secrets/service.py:180`, `user_store.py:234/435`, `server_store.py:59`. |
| Store readiness | User requires fingerprint configuration, selected row and successful decrypt. Server requires nonreserved allowlisted name, nonempty env value and fingerprint configuration. | Local plaintext may transiently exist during targeted decryption, but handler never calls `resolve/get_secret` for a value. `has_ref` does not return it. | Targeted cost/custody distinct from metadata-only inventory. No real secret used. |
| Handler result → finalizer | Listed payload status/reason replaced or unlisted two-field status built; `_discovery_result` preserves state and validates; dispatcher normalizes against current catalog. | ToolResult; no version advancement or component echo. | `_common.py:1285`, `_dispatch.py`. |
| Selected response → serializer | Declaration-selected parse reconstructs owned status; exact encoder is bound before heterogeneous egress. | Compose-loop tool message JSON; typed producer faults fail closed. | `response_contracts.py:35`, `discovery_response.py:60/69`, `tool_batch.py:3894`. |
| Audit / session / UI | Selected result and redacted display are separate representations; trace below. | A dispatcher return is not a committed receipt or browser observation. | Shared path source review only. |

### Representation and trust transitions

| From → to | Retained / dropped | Authority / custody |
|---|---|---|
| Model JSON → semantic args | Required exact name; invalid extra keys rejected. Decode conformity alone is not admission. | Model supplies a selector, not current user, resolver, destination permission or launch approval. |
| Scoped store reads → metadata | User/server precedence; selected readiness, no values or fingerprints in tool response. | Reference metadata remains data. A matching name is not proof of intent or authorization. |
| Mutable/frozen result → admitted status | Only exact two/five-field closed response; bool/string/types and reason coherence checked. | Nominal encoder binds interpretation. Metadata source_kind/name can still be arbitrary strings within owned service contract; no universal content sanitizer claim. |
| Admitted status → provider message | Exact selected data, normal envelope; no schema compact/fallback branch for this tool. | `tool_result_content_parts` grants no plugin-schema reference authority to secret results; guard still inspects content. |
| Selected message → redacted audit/reloaded history | Manifest projection and bounded storage representation can differ from transient selected data. | Names/scopes are classified as reference metadata, not credential values; this is not a claim that arbitrary candidate strings can never contain sensitive data. |

## CURRENT state, custody and lifecycle

### Dependencies, caches and lifetime

Composition state is immutable input; catalog/availability policy and resolver are request/server-owned context. The resolver binds auth provider to the service, while service configuration controls user-scope inclusion and server allowlist. User-store reads use separate DB connections; env readiness is read at probe time. No lease, value pin, selected fingerprint or atomic list/read snapshot is returned. Server configuration, secret save/delete, key availability/rotation or environmental change can alter the next answer.

The declaration forbids secret-discovery cacheability. Repeated synchronous calls execute list and has again, as tested; this does not promise the compose loop will permit redundant calls in one request. Loop repeat guards are a separate workflow restriction. No graph/store writes or provider calls appear in the handler. DB reads, selected decryption, graph validation, surrounding audit writes and enclosing provider attempts are real work; read-only graph effects are not zero physical activity.

No scope argument is present. `org` in the shared type does not establish an org backing store; the inspected service supports user and server selection. A corrupt/old-key user row shadows a ready same-name server entry. Explicitly scoped runtime resolution is another interface, not an alias for this check.

### Failure, cancellation and retry across boundaries

The synchronous handler has no cancellation checkpoints, retry loop or transactional rollback. It can finish inventory/decrypt work after surrounding cancellation. Normal freeform dispatch invokes it through `run_sync_in_worker` and `dispatch_with_audit`; malformed arguments yield ARG_ERROR feedback. Ordinary unexpected exceptions are captured as Composer plugin-crash outcomes, while audit integrity and designated interpreter/invariant failures propagate separately (`tool_batch.py`; the base locator `3139–3240` has drifted). This is source characterization, not fault-injection qualification of that async route.

The audited base also had a planner route (`run_planner_sync`, a cancelling discovery batch); U-30 retired it, and its cancellation characterization is not carried. Earlier/sibling reads and prior mutations may already have occurred. Neither cancellation nor an audit/error row proves rollback or no provider cost. Timeouts, late workers, request fencing and persisted failure cohorts remain `UNVERIFIED` by these synchronous tests; shared integration ownership is retained.

Rechecking can report changed readiness, but no retry makes a missing credential, malformed context, unauthorized destination or absent launch acknowledgement valid. Loop repetition policy may require structured repair instead of the same read again. There is no returned operation-specific commit witness because this tool changes no graph; surrounding request evidence and persistence have separate custody.

### Audit, persistence, reload and user-visible surfaces

| Event / representation | What it establishes / exposes | Limit and evidence |
|---|---|---|
| Selected ToolResult / admitted status | Exact current metadata at synchronous return. | Tested serializer data; not SDK delivery, browser display or persistence. |
| Invocation / visible-tool event | Shared dispatch records outcome, versions and argument identity. (The base's planner-side selected/display capture is retired with that route.) | `audit.py:1154` `dispatch_with_audit`, called from `tool_batch.py`; no invocation/audit DB run executed here. |
| Manifest audit projection | `validate_secret_ref` policy declares known key `name`, redacts unknown argument keys, handles reference metadata without value fields. Response bounded by common budgets. | `redaction.py:3603/4049/4178`; source only. This manifest does not mint disclosure permission or undo unexpected sensitive input. |
| `persist_turn_audit` | Serializes ToolResult through response walker; builds redacted tool rows and records state payload only for advancing versions. | `turn_audit.py:35/398/413`; handler itself never advances version. Surrounding transaction success is distinct from buffered outcome. |
| Prompt reload / current context | `prompts.build_messages` reconstructs history/current-state context using stored representations and current policy. | Full transient metadata is not promised as replayable history; exact row-to-API/UI and reload behavior not executed. |
| Secret REST / execution | REST validation and runtime resolution use different error/authority contracts; destination wiring and launch approval remain separate. | No live REST, execution, provider or browser operations performed. |

The LLM authors pipeline structure and the tutorial uses ordinary backend paths. This tool's metadata discovery introduces no structure synthesis, fallback authoring or tutorial exception.

## Formal source audit: agreement, known nonconformance and open questions

### Intended-to-current assessment

| Claim | Support / counterexample | Conclusion and limitation |
|---|---|---|
| `VS-C1`, `VS-C5` input and dialect | Required string, closed schema, local two-dialect compose-loop schema and exact-name cases. Direct handler order differs from public order, as documented. | Narrow `TESTED_PASS` controls; provider enforcement/final offering `UNVERIFIED`. No validate production repair identified. |
| `VS-C2`, `VS-C3` current access | Missing context refuses. Live has_ref overrides metadata. Real-store shared TC-013 controls show intact succeeds while corrupt/old-key fails despite listing true. | Distinction is functioning counterevidence to a validate readiness defect; auth/session/deployment acceptance not inferred. |
| `VS-C4` no values | Handler uses metadata and boolean; precise selected admission rejects extra `value`, invalid bool/scope and incoherent reason. | Narrow selected-egress controls pass. Backing user has_ref can transiently decrypt selected material; “no decryption anywhere” would be false. |
| `VS-C6`, `VS-C7` effects/errors | Exact unchanged state and store-call order tested. Faults propagate at synchronous seam; response admission rejects corruption. | Async cancellation, persistence and error-HTTP conversion remain source-only/unverified, no rollback claim. |

### KNOWN NONCONFORMANCE and supported disagreements

No new validate-specific implementation/declaration nonconformance was established in this bounded audit. This is a supported scoped clean result, not whole-tool approval.

**TC-013 remains the shared listing-promise finding.** Prior source-only `discovery_secrets` DS-01 identified that listing's `available` can reflect fingerprint-configured metadata rather than successful selected decryption. Existing tests deliberately prohibit per-row list decryption/material-column selection. Current shared real-store controls reproduce the distinction and show `validate_secret_ref` detects corruption/old key without falling through to a ready server secret. The appropriate listing wording repair belongs to its existing unit owner; this page neither weakens has_ref nor authorizes bulk decryption. No .10/#327 issue is duplicated or closed here.

Historical source pins from `67ad14c…` do not qualify this page at `9c22be98…`. The four prior Gold pages are historical source evidence only, not inherited runtime proof. Their full dossiers are not being migrated by this unit.

### UNVERIFIED questions and decisions

| Question / gap | Source already establishes | Needed discriminating evidence / owner |
|---|---|---|
| Atomic reason diagnosis | list and live probe are separate; old non-null reason is preserved if live false. | Controlled concurrent fingerprint/env/row change if atomic diagnostic truth is desired; product decision needed before claiming stronger semantics. No change proposed. |
| Unlisted false diagnosis | Two fields only, while conditional completion guidance generally asks to report a returned reason. | Keep explanation to what exists; separate product guidance decision if a universal reason is desired. No invented not-found reason. |
| Effective role/config/alias parity | The compose loop is the only builder; CLI MCP excludes it; inspected Advisor call has no tools. | Capture a real authorized request's selected config, prompt/overlay, adapted tools and delivery. None authorized/executed here. Alias inventory remains incomplete. |
| Privacy and audit/reload limits | Closed selected response and manifest projection exist. | End-to-end synthetic guard, audit persistence, retrieval and UI controls under shared owner; no claim of complete retention or leak absence. |
| Async fault/cancel/late-worker/race behavior | Shared worker and audit paths source traced. | Authorized controlled integration runs with actual request/session custody; not replaced by sync controls. |
| Current .10 integration drift | Immutable comparison to `.10` commit `51bb8131723baa822c1ce65ce1a981722d6c90f6` shows secret-module differences confined to wire model docstring/target_id guidance; interaction skill adds complete-value discovery/narrative. | This page and runs remain bound to `9c22be98…`. Disjoint validate symbols do not establish integrated whole-module/prompt/runtime parity; rerun/review integration after selected changes. |

## Verification and adversarial controls

### Existing evidence

On carry to `release/0.8.10-assembly` the [test module](../../../tests/unit/web/composer/test_validate_secret_ref_contract.py) was adapted to the retired planner route (two planner assertions dropped, as recorded in the matrix below) and run serially in that tree: 56 passed, exit 0. A mutation control applied in a scratch worktree (`resolve()` before inventory, and inventory availability in place of `has_ref`) turned it red; the carry report is lane-local. The paragraphs that follow describe the original audit at the base commit, and the r005 counts include five cases from other modules.

The [test module](../../../tests/unit/web/composer/test_validate_secret_ref_contract.py) exercises production builders, decoder, synchronous dispatch, selected response and serializers with a synthetic resolver that raises if asked to resolve values. The original normal runner r005 completed with **exit 0; JUnit `tests=61, failures=0, errors=0, skipped=0`**. After the run, the capture command re-read and hashed the same pre-enumerated 4,662 file paths, including the new formatted test, production source and recorded tests/config; it also reran HEAD and dirty-status queries. Those remeasured values matched their before-run values. The after JSON was initialized from the before JSON: timestamp, tree and import-path metadata were retained, and the file inventory was **not** re-enumerated. The recorded package imports were actually observed before the run, not measured again afterwards. Byte-identical JSON files therefore establish agreement of the remeasured fields, not independent after observations for every field or continuously frozen intermediate bytes. Its selected existing tests are `test_availability_discovery_remains_successful_metadata` (four active listed/unlisted × boolean cases) and `test_missing_context_does_not_reorder_argument_admission` (one case). Passing aggregate results alone do not qualify any unlisted claim.

Original logs, exits, JUnit, command/selectors, active case IDs and enumerated-file/HEAD/dirty measurements are retained under `.claude/lanes/tool011-active-20261021/validate_secret_ref/r005.*`; native locators, the capture-method qualification and digest-bound mapping are in the [binding companion](validate_secret_ref.binding.json). The original run artifacts remain unchanged by the `VSR-R1` documentation correction. r001–r004 are retained failed or incomplete test-authoring history, not current passing coverage. r001 had a wrong import, r002 a nonexistent factory, r003 omitted the production thaw step in the test, and r004 lacks its required before-binding receipt. None establishes a production defect.

Attributed shared TC-013 evidence is the list-unit normal r003 original run, exit 0, JUnit 39 passed with no failures/errors/skips. Its three active `test_metadata_readiness_is_not_live_resolution[intact|corrupt|old-key]` cases use real in-memory SQLite, UserSecretStore, ServerSecretStore, WebSecretService, ScopedSecretResolver, execute_tool and admitted serialization. Only that exact assertion scope is borrowed; no list-wide PASS is transferred. The shared secret module has the separately owned list-description correction; unchanged validate/model/response symbols and store dependencies were compared directly. Its source/test/config and original artifacts are separately bound in the companion. Earlier shared r002 is historical after test/description drift.

Repository checks for the new test completed separately. The g001 masquerade, walker-authority and mock-discipline selection recorded exit 1, 256 passes and two walker fixture failures: the expected synthetic `src/elspeth/real.py` was hidden by the containing checkout's `.claude/lanes/` ignore rule. `git check-ignore` confirmed that positive fixture case and returned no match for tracked production source and `AGENTS.md`. With unchanged source/test/config bytes, g002 reran only the 11 walker tests beneath an isolated fixture Git parent inside the project (pytest's basetemp was a child); all 11 passed, exit 0. The original g001 failures remain in evidence. Ruff check and format check exited 0. These repository checks do not add product-conformance coverage or qualify a full suite, PostgreSQL or operator signatures.

### Claim-to-control conformance matrix

All names below refer to active cases in r005 unless explicitly attributed. `TESTED_PASS` applies to the listed subclaim only; primary clauses retain the qualifications above.

| Subclaim / status | Specific assertions and active case IDs | Actual production path / boundary | Outcome and uncovered dimensions |
|---|---|---|---|
| `VS-C1-SHAPE` / `TESTED_PASS` | `test_invalid_shape_prevents_store_access[<direct|readonly|public-raise|public-return>-<omission|null|number|boolean|array|object|extra-scope>]`: typed failure or failed no-data result, zero list/has calls. Positive string calls in next row. | Handler Pydantic and public semantic-schema entry, before secret access. | Seven invalid shapes × four routes. Non-JSON Python coercion, arbitrary nesting and real API error projection not covered. |
| `VS-C5-BUILDERS` / `TESTED_PASS` | `test_exact_name_wire_and_dispatch[<ordinary|empty|whitespace|unicode|long>-<openai_strict|none>]`: exact schemas/descriptions, strict delta, decode conformity, exact name in actual resolver call and unlisted admitted status. The base's planner/freeform builder-equality assertion was dropped on carry: U-30 retired the planner builder, and comparing the loop entry with `wire_tool_definitions` would compare one function's output with itself. | Compose-loop builder → decoder → ordinary dict thaw → read-only dispatch → selected response. | Ten cases; no SDK/provider delivery, overlay or backing-store name creation guarantee. |
| `VS-C2-CONTEXT` / `TESTED_PASS` | `test_missing_authority_refuses_without_store_reads[user|service|both]`; existing admission-order test: no service reads, same state, exact code/message/no selected data, public schema wins over context guard. | Read-only/public guards and direct-handler ordering. | Server-owned missing context only; actual identity/auth-provider binding source-reviewed, not newly authenticated. |
| `VS-C3-STATUS` / `TESTED_PASS` | `test_targeted_status_replaces_inventory_availability[stored-fallback|env-fallback|server-fallback|env-kind-fallback|preserve-known-reason|clear-stale-reason]`; `test_unlisted_status_is_two_fields[False|True]`; existing four availability cases. Exact payload/serializer parity, list→has order, graph unchanged, outer success despite invalid graph. | Handler live probe / reason selection → finalizer → nominal selected response → `serialize_admitted_discovery_result` (the compose loop's serializer; the base's separate planner serializer assertion was dropped with that route). | Specific synthetic metadata variants; concurrent snapshot diagnosis not tested. |
| `VS-C4-EGRESS` / `TESTED_PASS` | `test_selected_egress_rejects_corrupt_or_extra_data[extra-value|nonboolean|incoherent-reason|invalid-scope]`; status cases supply intact positive controls. | Actual `_SECRET_STATUS_RESPONSE.admit` fails before selected encoding; no guessed field traversal. | Payload mutation is the negative control: a forbidden value field, bool impostor, missing reason or bad scope turns admission red. Does not qualify all guard/audit/UI sinks. |
| `VS-C6-READ` / `TESTED_PASS` | `test_secret_probe_is_not_cached_or_graph_authority`: no declared effect domains/cacheability, two calls hit live resolver, changed has result observed; status/fault cases compare unchanged graph. | Declaration and repeated synchronous production dispatch. | No graph write; real service lifetime, loop duplicate-policy and rollback across other calls untested. |
| `VS-C7-FAULT` / `TESTED_PASS` | `test_store_fault_is_not_missing_success[list|has]`: RuntimeError propagates; exact call-prefix and unchanged graph. | Production synchronous dispatch/handler; failures injected at dependency boundary deliberately. | No false missing success; async wrapper, HTTP, audit failure/cancel and storage exception class matrix untested. |
| `VS-C3-STORE-COUNTEREVIDENCE` / `TESTED_PASS`, attributed | List r003 `test_metadata_readiness_is_not_live_resolution[intact|corrupt|old-key]`: metadata list true; selected validate true only intact, failure reason for corrupt/old key; ready server shadowed; graph/version and value-canary checks in shared `_call`. | Real SQLite stores/service/scoped adapter → execute_tool → admitted wire. | Exact three cases at shared bound version; not arbitrary secret/privacy/provider/persistence acceptance. |
| `VS-GAP-ASYNC`, `VS-GAP-DELIVERY`, `VS-GAP-RELOAD` / `UNVERIFIED` | Controlled cancellation/late completion, final role/adapter provider request, guard failure, persisted audit+reload/UI comparisons: **NOT RUN**. | Actual worker/session/audit/provider boundaries need shared integration custody. | Synchronous control success cannot fill these gaps. |

### Prove this guide wrong

| Challenge | Discriminating setup / expected result | Control / execution status |
|---|---|---|
| Inventory readiness is enough | Use fingerprint-configured corrupt or old-key user ciphertext plus ready same-name server; list true, validate false. | Shared real-store intact positive plus corrupt/old-key mutations executed; supports targeted check, contradicts treating listing as live resolution. |
| Null means omission under strict | Supply `{"name":null}` under this tool; required field is not promoted. | Runtime schema rejection executed; no store access. Direct raw-W null classification is source-reviewed, no provider round trip. |
| Scope can be selected by extra key | Add `scope:"server"` to otherwise valid call. | All four synchronous admission variants reject before access. This protects against caller-selected shadow bypass; explicit-scoped runtime API is separate. |
| Live state is always fresh in reasons | Change global fingerprint config between inventory and has; earlier reason can survive even when a different live cause prevents readiness. | Finite race experiment specified, **NOT RUN**. Universal atomic diagnosis is not claimed. |
| Error means no work / cancellation rolls back | Inject has fault after list; cancel running selected decrypt at worker boundary and compare audit/fences. | List-before-fault prefix executed; async cancellation/late result **NOT RUN**. |
| Successful tool response equals full durable receipt | Compare selected long-name result, manifest projection, persisted tool row, reload and UI under common bounds. | **NOT RUN**. Common projection limits source-read only. |

## Version maintenance and evidence limits

Changing the handler, local argument model, declaration/description, response parser/encoder or shared SecretInventoryItem stales the relevant clauses. Changes to store precedence, fingerprint readiness/decryption, auth-provider scoping, disabled-user behavior, declaration effects/cacheability, schema/dialect/decode, role policy, prompt core/interaction/conditional guidance and overlays, finalization, guard labeling, audit/redaction/session/reload or worker cancellation also invalidate their dependent evidence. The binding companion uses the existing stable AST primitive, exact signature/body/file/Git identity and conservative whole-module dependencies; matching hashes prove source identity within that scope, never behavior or semantic equivalence.

Whole-module drift is conservatively visible even when a changed function is disjoint. The `.10` wire-only secret guidance and complete-value interaction-skill drift have been inspected through immutable Git comparison; this unit does not transplant whole old modules, refresh hashes to transfer coverage, or claim integrated parity. Integration must bind its final tree and rerun affected controls plus review. Production source is unchanged by this unit.

**Carry onto 0.8.10.** The binding companion is carried byte-identical from donor `60119def4`, like the `list_secret_refs` companion: it is declared bound to the audited base (`source_commit` `9c22be98…`), not rebound to this branch, and Markdown remains the edit authority. Its entries for `pipeline_planner.py` and its planner builder definitions describe the audited base; that route is retired here. Source line locators in this page are base-relative except those refreshed on carry (`tools/secrets.py`, `composition_completion.py`, `_common.py`, `discovery_response.py`, `provider_gateway.py`, `tool_batch.py`, `audit.py`).

### Confidence Assessment

Overall confidence is High for the exact source trace and explicitly executed synchronous controls, Moderate for any consequence depending on unexecuted enclosing lifecycle, and Insufficient Data for live delivery/deployment acceptance. Unsupported diagnoses and product decisions remain visible above.

### Risk Assessment

The documentation/test-only change has Low implementation risk and is easily reversible. The consequential risk is overreading point-in-time readiness as wiring permission or launch proof, or treating metadata inventory/old runtime receipts as stronger evidence. The guide keeps those boundaries explicit; tests use synthetic material and preserve production custody.

### Information Gaps

Effective deployment overlays, request role/config/alias populations, actual adapter delivery, authenticated session admission, concurrent reason diagnosis, cancellation and audit/reload/UI qualification remain missing. No providers, production secrets, live credential stores or deployment operations were used.

### Caveats and Required Follow-ups

The completed independent review read the complete page, binding companion, tests and original evidence; its focused consistency check resolved `VSR-R1`. This candidate does not inherit Gold status. Any later repair must update source, declaration/guidance, complete contract, meaningful controls and bound receipts together; source re-review alone cannot restore tested behavior after relevant drift.
