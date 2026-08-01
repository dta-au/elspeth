# Source-Native Single-Prompt LLM Plugin

**Date:** 2026-08-01
**Status:** User-approved architecture; document review pending
**Target:** ELSPETH `release/0.7.2`
**Branch:** `codex/llm-source`
**Base commit:** `63d713dc4bdca3d07f32352eb9464ad9dce1e96c`
**Tracker:** `elspeth-0b075bdf2b`, `elspeth-b35b10722c`

## Decision

Add a real ELSPETH source plugin named `llm`. It accepts an author-supplied
prompt, performs exactly one provider request without consuming any input rows,
and emits exactly one `SourceRow`. The row contains the same three operational
fields produced by the existing `llm` transform:

- the configured response field;
- `<response_field>_usage`; and
- `<response_field>_model`.

The source supports the same provider families as the transform: Azure OpenAI,
OpenRouter, Amazon Bedrock, and the ELSPETH compatibility gateway. It shares
provider adapters, response validation, audit vocabulary, and output-field
helpers where those contracts are genuinely common, but it does not construct
an `LLMTransform`, synthesize an empty pipeline row, or route through transform
execution.

Because the source has LLM egress and model-generated output, it declares
`PluginCapability.LLM`. Required-control coverage, Composer auto-wiring,
execution admission, audit readiness, and consent summaries must treat that
capability consistently for both source and transform components before the
plugin can ship.

## Context

The existing `llm` transform is row-oriented. Its prompt rendering, audit
parentage, error routing, query pooling, and schema propagation all assume an
input `PipelineRow` with a state and token. Creating a fake row would make the
source appear to have provenance that never existed, and instantiating the
transform inside a source would couple two plugin lifecycles and make policy
classification ambiguous.

ELSPETH's source executor already supplies the correct lifecycle for this use
case. It opens a `source_load` Landscape operation before calling `load()`, then
creates the emitted row's state and token only after the source yields. The
provider request must therefore be audited under that real operation rather
than under invented row identifiers.

The security parity sweep also found a pre-existing assumption that LLM
capability exists only on transform nodes. On a deployment where Content Safety
is required, an LLM source could otherwise generate text without a coverage
finding or an auto-wired control. That P1 defect is a prerequisite of this
feature rather than follow-up work.

## Goals

1. Make a static, author-supplied prompt usable as a source with no input rows.
2. Perform one and only one provider request per source execution.
3. Emit one row with configurable response, usage, and model field names that
   follow the existing LLM transform convention.
4. Support Azure OpenAI, OpenRouter, Bedrock, and the ELSPETH gateway through a
   provider-discriminated configuration schema.
5. Preserve fail-closed provider-response and finish-reason validation.
6. Record the external request and its logical/transport audit records beneath
   the source's real `source_load` operation.
7. Make source LLM egress visible to policy, execution admission, audit
   readiness, and operator consent surfaces.
8. Require Content Safety to post-dominate the source's generated-text output
   on every downstream route when that control is configured as required.
9. Keep YAML, plugin discovery, catalogue, and Web Composer authoring surfaces
   consistent with other source plugins.

## Non-goals

V1 does not include:

- row-derived prompts or a synthetic input row;
- transform queries, named multi-query results, query pools, batch sizing, or
  per-row retry/error routing;
- streaming provider responses;
- conversation history or tool calls;
- multiple output rows;
- source-managed scheduling or polling;
- a new provider family;
- automatic Prompt Shield insertion for the authored source prompt; or
- clearing the package-wide judge-signature gate.

## Public configuration contract

### Representative configuration

```yaml
sources:
  - name: generate_topic
    plugin: llm
    on_success: generated
    options:
      provider: openrouter
      model: openai/gpt-4o-mini
      prompt_template: >-
        Write one concise research question about trustworthy data pipelines.
      system_prompt: You are a careful research assistant.
      temperature: 0.2
      max_tokens: 300
      response_field: research_question
      on_validation_failure: discard
      schema:
        mode: observed
```

The emitted row is shaped as follows:

```yaml
research_question: "..."
research_question_usage:
  prompt_tokens: 18
  completion_tokens: 24
  total_tokens: 42
research_question_model: openai/gpt-4o-mini
```

The precise usage object remains provider-dependent in the same bounded,
validated form accepted by the transform. Missing provider usage is represented
using the transform's existing operational-field convention rather than by
omitting the configured field.

### Common fields

| Field | Type and default | Contract |
|---|---|---|
| `provider` | required discriminator | One of `azure`, `openrouter`, `bedrock`, or `gateway`, using the existing transform vocabulary exactly. |
| `prompt_template` | required non-empty string | Rendered once from author-controlled template globals; row access is forbidden. |
| `system_prompt` | optional string | Provider system instruction, with the same provider support and validation as the transform. |
| `temperature` | bounded float | Same bounds and default as the corresponding transform provider config. |
| `max_tokens` | positive integer | Same bounds and default as the corresponding transform provider config. |
| `response_field` | non-empty field name; default `llm_response` | Base name for the response and the two operational fields. |
| `on_validation_failure` | required route or `discard` | Existing source-boundary policy for a completed row that contradicts its declared schema. |
| `schema` | `SchemaConfig` | Existing source schema contract, augmented by the three guaranteed LLM output fields. |

Each provider variant also exposes the same model, endpoint, authentication,
timeout, and provider-specific options as its transform counterpart when those
options apply to a single request. Transform-only fields are not accepted:
`required_input_fields`, `queries`, pool configuration, batch configuration,
and row retry/error-output options are absent from the source schema.

The full JSON schema is a provider-discriminated `oneOf`, not the schema for
only a default provider. Catalogue examples and agent-assistance content must
show all four variants without exposing resolved credentials.

### Prompt rendering

`prompt_template` is rendered once through ELSPETH's sandboxed template
environment. It may use the same explicitly supported, author-controlled
lookup/config globals as other templates. It may not reference `row`, because
there is no row at source time. Validation rejects a row-dependent template
before execution; the implementation does not satisfy it with `{}` or another
sentinel object.

The rendered prompt must be non-empty after whitespace handling. The rendered
prompt, not merely the template source, is subject to the existing provider
request bounds.

## Plugin and provider architecture

### Source plugin

`LLMSource` implements `BaseSource` directly and is discovered from
`src/elspeth/plugins/sources/llm/`. Its contract is:

1. validate the source-specific provider configuration;
2. construct the selected provider during `on_start()`;
3. render the prompt and make one provider request during `load()`;
4. validate the provider result and populate the three output fields;
5. validate the completed mapping against the source output schema;
6. yield `SourceRow.valid(..., contract=contract, source_row_index=0)`; and
7. close provider resources during `close()`.

The plugin declares non-deterministic execution and the same relevant audit
characteristics as the transform, including LLM/external egress and credentials
where applicable. It also declares `PluginCapability.LLM` through
`policy_capabilities()`.

### Shared code boundary

Provider adapters and pure helpers are shared. Plugin execution is not.

Common code may include:

- provider construction and provider-specific request/response adapters;
- response and finish-reason validation;
- response-field naming and guaranteed-field construction;
- usage/model operational-field population;
- markdown-fence normalization; and
- provider-neutral prompt rendering helpers that do not require a row.

Source configuration models are rooted in a source/data configuration base,
not the transform's `LLMConfig` hierarchy. Shared mixins or validators may be
extracted so provider fields remain consistent without inheriting transform-only
options.

The source must not instantiate `LLMTransform`, invoke its `process()` method,
construct a `PipelineRow`, or create a synthetic state/token.

## Audit parentage

The existing provider interface assumes a row state and token. Extend the
shared provider seam with an owned, discriminated audit-parent value having
exactly two valid forms:

- row parent: `state_id` and `token_id`; or
- operation parent: `operation_id`, with no state or token.

Construction rejects mixed or empty parentage. The transform continues to use
the row form with no change to its audit semantics. The source obtains the
operation identifier from `SourceContext` while `load()` runs and uses the
operation form.

Every logical and transport external-call record for the source request must
bind to the same `source_load` operation. No source provider call may occur
outside an active operation, and no placeholder state/token may be persisted.
Failed provider calls remain recorded through the existing audited-client
failure path before the source load fails.

## Output and schema semantics

For `response_field: answer`, the source guarantees:

- `answer`: validated model text;
- `answer_usage`: validated usage metadata in the transform-compatible shape;
  and
- `answer_model`: the validated effective model identifier.

The three names must be distinct from one another and valid under the existing
field-name rules. The declared source output contract includes all three fields
regardless of whether the configured schema mode is fixed, flexible, or
observed. User schema declarations cannot contradict their required types.

The source emits no implicit prompt field and no provider-native response
object. Audit records retain the bounded request/response evidence already
captured by the provider layer. The text normalization and operational-field
semantics remain byte-for-byte compatible with the transform helpers so a
downstream component can consume source and transform LLM outputs uniformly.

## Failure and lifecycle semantics

- Configuration and template errors fail before the provider request.
- Provider response shapes are parsed as external data and fail closed.
- Empty model text and non-success finish reasons use the same typed failures as
  the transform/provider path.
- A provider failure fails the source load and produces no row; there is no
  input row to route to a transform error stream.
- The provider is closed exactly once on normal completion, provider failure,
  cancellation, or downstream run failure.
- Source execution performs no hidden preflight request. The single real
  request is authoritative.

## Required-control and policy parity

### Content Safety ruling

Content Safety applies. The source produces model-generated text and must not
create an unguarded route when `content_safety` is `REQUIRED`.

Coverage discovery must inspect both `state.sources` and `state.nodes` through
component-appropriate capability lookup. For an LLM source, the protected field
set is the configured response field only; usage and model metadata are not
model-generated text.

Coverage begins at the source's `on_success` stream. Every downstream path from
that stream to a terminal sink must encounter an applicable Content Safety
control for the response field. This is source post-domination: the control is
downstream of the source and must cover every branch, rather than sitting
between an upstream and downstream transform node.

Composer auto-wiring repairs an uncovered source by:

1. preserving the source's previous `on_success` stream as the control's
   success destination;
2. allocating a new intermediate stream;
3. changing only that source's `on_success` to the intermediate stream; and
4. inserting a Content Safety node that consumes the intermediate stream and
   writes success to the preserved destination.

The splice must preserve named-source identity, be idempotent, handle multiple
LLM sources independently, and leave the candidate object unchanged when no
repair is necessary. Budgeting and trigger counts include LLM sources as well
as transform nodes.

### Prompt Shield ruling

Prompt Shield does not auto-apply to the V1 source prompt. It protects model
instructions from untrusted row-derived content; this source has no input row,
and its prompt plus lookup/config interpolation are author-controlled
configuration. This is an explicit non-applicability ruling, not an omission.
If a future source variant imports external content into the prompt, its design
must revisit Prompt Shield before admission.

### Parity sweep

The implementation must update and test every current transform-only LLM
discriminator:

- plugin-policy capability lookup and required-control coverage;
- Composer required-control detection, budget, and splice targeting;
- execution-time base-URL, tracing, and required-control admission checks;
- retry-budget checks only where the source actually exposes retry settings;
- audit-readiness external-boundary and LLM-explanation rows;
- readiness summaries that define whether an LLM exists in the composition;
- Execute consent-dialog LLM egress enumeration and uncertainty handling; and
- catalogue/authoring classification of external-call and secret requirements.

Interpretation review that specifically explains row-to-LLM transform semantics
remains transform-only. Any retained transform-only site must have a focused
test or an explanatory comment establishing why source semantics do not apply.

## Web and catalogue behavior

The source appears in the source catalogue with:

- all four provider-specific configuration schemas;
- its one-row output contract;
- non-deterministic and external-egress characteristics;
- deferred-secret requirements by provider;
- the three guaranteed output fields; and
- authoring guidance that the prompt is static and row access is invalid.

The Execute consent dialog reports that the source sends its authored prompt to
the selected LLM provider. It must not say that the source sends pipeline rows.
If catalogue information is unavailable, the dialog retains the existing
fail-closed/uncertain egress presentation rather than silently omitting the
source.

## Test strategy

Implementation proceeds test-first. The minimum regression matrix is:

1. **Configuration:** provider-discriminated schema for all four providers;
   default/custom response field; invalid and colliding field names; rejection
   of transform-only options; row-dependent and empty prompts.
2. **Source lifecycle:** one request, one row at index zero, three fields,
   provider setup/close, cancellation, provider error, malformed response,
   empty content, and non-success finish reason.
3. **Provider parity:** Azure, OpenRouter, Bedrock, and gateway construction and
   request mapping using audited fakes; no live credentials required.
4. **Audit integrity:** operation-parent records for success and failure;
   transform row-parent regression coverage; rejection of mixed/missing audit
   parentage; no fabricated state/token.
5. **Schema propagation:** source guaranteed fields across schema modes and
   downstream consumption of response/usage/model fields.
6. **Required controls:** uncovered-source finding, fully covered path, one
   uncovered branch, custom response field, source-only composition, mixed
   source/transform composition, multiple sources, idempotent auto-wire, stable
   no-op identity, and named-source splice preservation.
7. **Prompt Shield:** explicit non-applicability for the source and unchanged
   transform behavior.
8. **Execution policy:** source base URL and tracing admission, generic
   required-control rejection, and retry-budget non-applicability unless retry
   settings are deliberately exposed.
9. **Audit readiness and consent:** LLM source external-boundary detail,
   source-specific prose, all provider classifications, and uncertain catalogue
   behavior.
10. **Discovery/catalogue:** source auto-discovery, configuration schema golden,
    reference content, value-source declarations, and credential redaction.

Before handoff, run focused tests during red/green development, then:

```bash
.venv/bin/python -m pytest tests/
elspeth-lints check
.venv/bin/python scripts/wardline_gate.py
```

The package-wide signed trust-tier allowlist may remain in its documented
fail-closed state; this change must not add tier-model defects or drift.

## Expected implementation areas

The plan may refine exact file boundaries, but implementation is expected to
touch:

- `src/elspeth/plugins/sources/llm/` for the source and source configs;
- `src/elspeth/plugins/transforms/llm/` for narrowly shared provider/audit
  primitives and pure helpers;
- plugin discovery/catalogue and source authoring surfaces;
- `src/elspeth/web/plugin_policy/coverage.py`;
- `src/elspeth/web/composer/required_controls.py`;
- `src/elspeth/web/execution/validation.py`;
- `src/elspeth/web/audit_readiness/`;
- `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx`; and
- focused unit/integration/golden tests for each contract above.

No production implementation begins until this document is reviewed. After
review, the next artifact is a task-level implementation plan with explicit
test-first checkpoints.

## Rejected alternatives

### Wrap the LLM transform

Rejected because it creates two plugin lifecycles, imports row-only config into
the source, and obscures which component owns policy and audit behavior.

### Synthesize an empty row

Rejected because it fabricates state/token provenance and permits accidental
row-template behavior that the source contract forbids.

### Add a zero-row mode to the transform

Rejected because transforms are graph nodes that consume streams. A mode switch
would weaken plugin-kind invariants and still leave source discovery, catalogue,
and required-control semantics unresolved.

### Ship the source before policy parity

Rejected because required Content Safety would be bypassable on source-only
LLM compositions. The P1 parity repair is a prerequisite of feature completion.
