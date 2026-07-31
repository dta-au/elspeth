# Configuration Reference

Complete reference for ELSPETH pipeline configuration.

---

## Table of Contents

- [Configuration File Format](#configuration-file-format)
- [Top-Level Settings](#top-level-settings)
- [Web Plugin Policy](#web-plugin-policy)
- [Secrets Settings](#secrets-settings)
- [Source Settings](#source-settings)
- [Queue Settings](#queue-settings)
- [Sink Settings](#sink-settings)
- [Transform Settings](#transform-settings)
  - [LLM transform (`llm`)](#llm-transform-llm)
- [Gate Settings](#gate-settings)
- [Aggregation Settings](#aggregation-settings)
- [Coalesce Settings](#coalesce-settings)
- [Pipeline Dependencies](#pipeline-dependencies)
- [Commencement Gates](#commencement-gates)
- [Collection Probes](#collection-probes)
- [Landscape Settings (Audit Trail)](#landscape-settings-audit-trail)
- [Concurrency Settings](#concurrency-settings)
- [Rate Limit Settings](#rate-limit-settings)
- [Telemetry Settings](#telemetry-settings)
- [Checkpoint Settings](#checkpoint-settings)
- [Retry Settings](#retry-settings)
- [Payload Store Settings](#payload-store-settings)
- [Environment Variables](#environment-variables)
- [Expression Syntax](#expression-syntax)
- [Complete Example](#complete-example)

---

## Configuration File Format

ELSPETH uses YAML configuration files with environment variable expansion:

```yaml
# Standard variable expansion
database_url: ${DATABASE_URL}

# With default value
database_url: ${DATABASE_URL:-sqlite:///./audit.db}
```

Configuration is loaded with this precedence (highest first):
1. Environment variables (`ELSPETH_*`)
2. Config file (settings.yaml)
3. Pydantic schema defaults

Nested environment variables use double underscore: `ELSPETH_LANDSCAPE__URL`.

---

## Top-Level Settings

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `sources` | object | **Yes** | - | Named source plugin configurations (one or more per run; map of source name → source config) |
| `sinks` | object | **Yes** | - | Named sink configurations (at least one required) |
| `queues` | object | No | `{}` | Named pass-through queue nodes for explicit fan-in (required when multiple producers feed one processing node) |
| `run_mode` | string | No | `"live"` | Execution mode: `live`, `replay`, `verify` |
| `replay_from` | string | No | - | Run ID to replay/verify against (required for replay/verify modes) |
| `transforms` | list | No | `[]` | Ordered transforms to apply |
| `gates` | list | No | `[]` | Config-driven routing gates |
| `coalesce` | list | No | `[]` | Fork path merge configurations |
| `aggregations` | list | No | `[]` | Batch processing configurations |
| `depends_on` | list | No | `[]` | Pipeline dependencies — run these before the main pipeline |
| `commencement_gates` | list | No | `[]` | Go/no-go conditions evaluated after dependencies complete |
| `collection_probes` | list | No | `[]` | Vector store collections to probe for gate context |
| `landscape` | object | No | (defaults) | Audit trail configuration |
| `concurrency` | object | No | (defaults) | Parallel processing settings |
| `retry` | object | No | (defaults) | Retry behavior settings |
| `payload_store` | object | No | (defaults) | Large blob storage settings |
| `checkpoint` | object | No | (defaults) | Crash recovery settings |
| `rate_limit` | object | No | (defaults) | External call rate limiting |
| `telemetry` | object | No | (defaults) | Telemetry export configuration |
| `secrets` | object | No | (defaults) | Secret loading configuration |

### Run Modes

| Mode | Behavior |
|------|----------|
| `live` | Execute normally, make real external calls |
| `replay` | Use recorded responses from a previous run |
| `verify` | Compare new results against a previous run |

---

## Web Plugin Policy

The web service compiles one operator-owned plugin policy at startup. Its
required authorization set is:

- sources: `source:csv`, `source:json`, `source:text`
- transforms: `transform:field_mapper`, `transform:llm`,
  `transform:web_scrape`
- sinks: `sink:csv`, `sink:json`, `sink:text`

The operator-profiled LLM is request-available only when that principal has at
least one usable configured profile. Missing LLM or tutorial credentials do
not hide CSV/JSON/text authoring.

Authorize every optional web plugin with its kind-qualified ID in
`plugin_allowlist`. An installed plugin that is absent from this list remains
available to CLI and batch runs but is hidden from every web discovery,
authoring, import, validation, and execution surface.

The required authorization set above is always authorized regardless of
`plugin_allowlist`; the allowlist only adds optional plugins on top of it,
and repeating a required ID is harmless. An empty or unset allowlist
therefore authorizes exactly the required core — it does not mean "allow
every installed plugin". Startup fails when an allowlisted plugin is not
installed.

The Composer's own chat and advisor models are configured separately from
the plugin policy (`ELSPETH_WEB__COMPOSER_MODEL`,
`ELSPETH_WEB__COMPOSER_ADVISOR_MODEL`). See
[Web LLM Configuration](environment-variables.md#web-llm-configuration) for
how every web LLM surface — Composer models, operator LLM profiles, the
tutorial profile, and the allowlist — fits together.

### Environment configuration

Collection and mapping values use JSON. This example enables the AWS Bedrock
prompt and output controls, requires both controls, and selects a keyless
Bedrock profile for the tutorial:

> **This is the plain-environment-file form** — systemd, Docker Compose, or any
> deployment where you set these variables yourself. It is **not** the AWS ECS
> form. There, the Terraform scenario module creates both Guardrails and renders
> their identifiers for you, so the hand-written `guardrail_identifier` and
> `guardrail_version` values below would name Guardrails nothing owns. Configure
> that deployment through the module's variables instead — see
> [Where each deployment sets these](environment-variables.md#where-each-deployment-sets-these).

```bash
ELSPETH_WEB__PLUGIN_ALLOWLIST='["transform:aws_bedrock_prompt_shield","transform:aws_bedrock_content_safety"]'
ELSPETH_WEB__PLUGIN_PREFERENCES='{"prompt_shield":["transform:aws_bedrock_prompt_shield"],"content_safety":["transform:aws_bedrock_content_safety"]}'
ELSPETH_WEB__PLUGIN_CONTROL_MODES='{"prompt_shield":"required","content_safety":"required"}'
ELSPETH_WEB__LLM_PROFILES='{"standard":{"provider":"bedrock","model":"bedrock/anthropic.claude-3-haiku-20240307-v1:0","region_name":"ap-southeast-2"}}'
ELSPETH_WEB__DEFAULT_LLM_PROFILE='standard'
ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES='[{"alias":"prompt-default","plugin":"aws_bedrock_prompt_shield","guardrail_identifier":"operatorpromptguardrail","guardrail_version":"7","region":"ap-southeast-2"},{"alias":"content-default","plugin":"aws_bedrock_content_safety","guardrail_identifier":"operatorcontentguardrail","guardrail_version":"4","region":"ap-southeast-2"}]'
ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES='{"aws_bedrock_prompt_shield":"prompt-default","aws_bedrock_content_safety":"content-default"}'
```

AWS ECS acceptance treats those seven strings as one protected assignment.
The controller hashes their exact raw values, stores the binding in the bound
scenario inventory, and compares every returned candidate/verifier task
definition byte-for-byte with that inventory, together with the live Bedrock
model and AWS region. The Guardrail receipt and durable receipt store must
match the same protected binding; a substituted bundle with a recomputed hash
does not satisfy the contract.

Preference arrays are ordered. When a capability has multiple authorized
implementations, list every implementation exactly once in the desired order.
`required` mode blocks a pipeline unless the selected implementation is
available and covers every applicable path. `recommend` mode keeps the
control advisory.

An OpenRouter or Azure LLM profile must declare `credential_scope` and an
operator-owned `credential_ref`. A server-scoped profile resolves only through
the server store; a user-scoped profile resolves only through that principal's
store. Web-authored pipeline state stores the opaque profile alias, not the
provider, model, endpoint, or credential binding.

`ELSPETH_WEB__DEFAULT_LLM_PROFILE` must name a configured profile or the
service refuses to start — so renaming or removing a profile this still points
at will break a previously healthy deployment on its next restart; change both
together. The tutorial needs *a* profile, not a dedicated one: point it at an
ordinary general-purpose alias rather than inventing a `tutorial` alias, which
would otherwise appear in every unrelated pipeline's audit trail. It is also the
alias listed first for authors and used by the Composer's worked examples, so it
should name a general-purpose tier. Setting it enables the first-run tutorial, whose
launch contract needs more than the profile alone: the tutorial pipeline is
exactly one `csv`/`json` source, the `web_scrape`, `llm`, and `field_mapper`
transforms, and one `json` sink, and every one of those plugins must be
installed and available (with required-control coverage satisfied when
prompt-shield/content-safety modes are `required`). The three tutorial
transforms are part of the required core, but keep them listed explicitly in
deployment allowlist templates so the tutorial's dependency set stays
visible, and verify the tutorial rows in `GET /api/system/status` after any
policy change. See
[Web LLM Configuration](environment-variables.md#web-llm-configuration).

Bedrock Guardrail profiles follow the same separation. Web authors select only
an opaque `profile`, row `fields`, `schema`, and, for content safety, `source`.
The operator-owned Guardrail identifier, immutable numeric version, and region
are lowered only for validation and execution. When a plugin has multiple
profiles, configure its choice explicitly in
`ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES`; plugin preferences select the
implementation, while this mapping selects that implementation's private
binding. Credentials come from the AWS default SDK chain. On ECS, grant the
required Bedrock permissions to the task role; never place access keys or
custom endpoints in profile or pipeline configuration.

### Startup, restart, and remediation

Restart the web service after changing the allowlist, preferences, control
modes, profile definitions, or tutorial profile. The process policy is frozen
after startup; secret availability is recomputed per principal and again before
execution.

Startup fails with a sanitized configuration error when the core is missing,
an ID is malformed or duplicated, an authorized plugin is not installed or
locally usable, a required capability has no implementation, a preference
order is incomplete, or an authorized plugin lacks canonical version/hash
identity. Correct the named setting or plugin ID and restart; the error does
not echo raw JSON or private profile bindings.

Saved states that reference a now-disabled plugin remain readable and
exportable in their authored form. Remove or replace each component using the
web repair action before validation or execution. The server does not fetch a
hidden plugin schema and does not silently re-enable the component.

### Readiness and audit evidence

`GET /api/system/status` exposes separate sanitized rows for policy
compilation, required core, local capability configuration, live provider
health, tutorial profile configuration, and tutorial required-control
coverage. A missing tutorial profile disables the tutorial without hiding
ordinary CSV/JSON/text authoring. The authenticated launch path rechecks the
principal's profile credential and the complete tutorial candidate immediately
before creating a run; a failed check returns a typed HTTP 409.

Every web run records the policy hash, principal snapshot hash, authorized and
available IDs, selected implementations, safe profile aliases, plugin code
identities, and closed decision codes introduced in Landscape epoch 23 and
retained in epoch 25. Readiness,
errors, logs, telemetry, persisted state, and exports omit private profile
bindings.

Every validation or execution request receives a principal-scoped availability
snapshot. Execution freezes one fresh snapshot before creating the run, and
the same snapshot controls initial plugin construction and delayed export-sink
construction. A saved pipeline whose plugin has since been disabled is rejected
before a run or plugin instance is created.

Operator-profiled web plugins expose only an opaque `profile` alias plus safe
pipeline options. Provider, model, endpoint, and credential bindings are
lowered in memory for execution; Landscape run/node configuration and exported
audit data retain the authored alias form. CLI and batch configuration remain
explicit and unrestricted by web policy: `instantiate_plugins_from_config()`
and `make_sink_factory()` consume normal pipeline settings without `WebSettings`
or a web availability snapshot.

---

## Secrets Settings

Configure how secrets (API keys, tokens) are loaded for the pipeline.

```yaml
secrets:
  source: keyvault
  vault_url: https://my-vault.vault.azure.net  # Must be literal URL, no ${VAR}
  mapping:
    AZURE_OPENAI_KEY: azure-openai-key
    AZURE_OPENAI_ENDPOINT: openai-endpoint
    ELSPETH_FINGERPRINT_KEY: elspeth-fingerprint-key
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `source` | string | No | `"env"` | Secret source: `env` or `keyvault` |
| `vault_url` | string | When `source: keyvault` | - | Azure Key Vault URL (must be literal HTTPS URL) |
| `mapping` | object | When `source: keyvault` | - | Env var name → Key Vault secret name |

### Source Options

| Source | Behavior |
|--------|----------|
| `env` | Secrets come from environment variables / .env file (default) |
| `keyvault` | Secrets loaded from Azure Key Vault using explicit mapping |

**Important:** `vault_url` must be a **literal URL** like `https://my-vault.vault.azure.net`. Environment variable references like `${AZURE_KEYVAULT_URL}` are **not supported** because secrets must be loaded before environment variable resolution occurs.

### Authentication (Key Vault)

Uses Azure DefaultAzureCredential, which tries (in order):
1. Managed Identity (Azure VMs, App Service, AKS)
2. Azure CLI (`az login`)
3. Environment variables (`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`)
4. Visual Studio Code Azure extension

### Examples

**Local Development:**

```yaml
# Use .env file for local development
secrets:
  source: env
```

**Production with Key Vault:**

```yaml
secrets:
  source: keyvault
  vault_url: https://prod-vault.vault.azure.net
  mapping:
    AZURE_OPENAI_KEY: azure-openai-api-key
    AZURE_OPENAI_ENDPOINT: azure-openai-endpoint
    ELSPETH_FINGERPRINT_KEY: elspeth-fingerprint-key
```

---

## Source Settings

Configures the pipeline's data sources. Every pipeline declares one or more
named sources under the `sources:` key; each entry maps a source name to a
source plugin configuration.

```yaml
sources:
  transactions:
    plugin: csv
    on_success: source_out    # Explicit output connection name
    options:
      path: data/input.csv
      schema:
        mode: fixed
        fields:
          - "id: int"
          - "amount: int"
      on_validation_failure: quarantine
```

Per-source fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plugin` | string | **Yes** | Plugin name: `csv`, `json`, `text`, `aws_s3`, `azure_blob`, `dataverse`, `null` |
| `on_success` | string | **Yes** | Connection name for source output (transforms reference this via `input`) |
| `options` | object | No | Plugin-specific configuration |

**`options.path` on the web surface.** For file sources (`csv`, `json`, `text`)
`path` is an ordinary filesystem path when the pipeline runs under the CLI. A
web-authored source instead references a file uploaded to the session:
`path: blob:<uuid>`, where the UUID identifies that session's uploaded blob.
Filesystem paths are not free-form on the web surface — a source may only read
from the session's own subtree of the deployment blob directory, and a request
without a session identity gets no readable local path at all — so referencing
the upload by its `blob:<uuid>` sentinel is the practical way to author a file
source there.

Source names are durable, audit-visible identifiers: they become DAG node IDs
and appear in audit records (`run_sources`, `source_node_id`). Names must be
lowercase, unique across all node names (transforms, gates, aggregations,
coalesce nodes, queues, sinks), must not start with `__`, and must not use
reserved edge labels.

The legacy singular `source:` key is deleted, not deprecated (ADR-025).
A configuration that supplies `source:` fails validation with an error
directing the operator to rewrite it as `sources: { <name>: { ... } }`.

### Available Source Plugins

| Plugin | Purpose |
|--------|---------|
| `csv` | Load from CSV file |
| `json` | Load from JSON file or JSONL |
| `text` | Load one output row per text line into a configured column |
| `aws_s3` | Load bounded CSV, JSON-array, or JSONL rows from one immutable S3 object (CLI and batch only — see [AWS S3 sink](#aws-s3-sink)) |
| `azure_blob` | Load from Azure Blob Storage |
| `dataverse` | Load from Microsoft Dataverse via OData v4 REST API |
| `null` | Empty source (for testing) |

### Multi-Source Pipelines

A run may declare multiple named sources. Cross-source ingest is sequential
by design: the engine iterates sources one at a time in YAML declaration
order. Declaration order is the determinism anchor — source iteration is not
concurrent, and a later source's rows are not admitted until the previous
source is exhausted. Every row carries durable per-source provenance:

- `source_node_id` — which source node the row entered from
- `source_row_index` — the row's position within its own source (restarts at 0 per source)
- `ingest_sequence` — the row's global admission position across all sources

Each source's schema is validated independently under its own contract;
contracts do not bleed between sources, and the per-source contract is
persisted in the `run_sources` audit table.

When two or more producers (for example, two sources) feed the same ordinary
processing node, the fan-in must be made explicit with a queue node (see
[Queue Settings](#queue-settings)); graph validation rejects implicit fan-in.
Sinks and coalesce nodes accept fan-in directly.

```yaml
sources:
  orders:
    plugin: csv
    on_success: inbound
    options:
      path: input/orders.csv
      schema:
        mode: observed
  refunds:
    plugin: csv
    on_success: inbound
    options:
      path: input/refunds.csv
      schema:
        mode: observed

queues:
  inbound: {}    # Both sources publish to 'inbound'; the queue makes the fan-in explicit

transforms:
  - name: normalize_rows
    plugin: passthrough
    input: inbound
    on_success: combined
    on_error: discard
    options:
      schema:
        mode: observed
```

Working examples: `examples/multi_source_queue/` (two sources fanning into a
shared transform path) and `examples/multi_flow/` (independent per-source
flows). Design rationale: ADR-025 (multi-source ingestion) and ADR-026
(durable token scheduler).

### Schema Options

```yaml
schema:
  mode: fixed          # fixed, flexible, or observed
  fields:
    - "id: int"       # Field name and type
    - "name: str"
    - "amount: float"
on_validation_failure: quarantine  # quarantine or discard
```

| Schema Mode | Behavior |
|-------------|----------|
| `fixed` | Require exactly the specified fields (extras rejected) |
| `flexible` | At least these fields must be present (extras allowed) |
| `observed` | Infer schema from data (no explicit field definitions) |

### Schema Contracts (DAG Validation)

For observed schemas that still have field requirements, use contract fields:

```yaml
# Producer guarantees these fields will exist in output
schema:
  mode: observed
  guaranteed_fields: [customer_id, timestamp, amount]

# Consumer requires these fields in input
schema:
  mode: observed
  required_fields: [customer_id, amount]
```

| Field | Purpose |
|-------|---------|
| `guaranteed_fields` | Fields the producer guarantees will exist (for observed schemas) |
| `required_fields` | Fields the consumer requires in input (for observed schemas) |

The DAG validates at construction time that upstream `guaranteed_fields` satisfy downstream `required_fields`. For explicit schemas (`mode: fixed` or `flexible`), declared fields are implicitly guaranteed.

---

## Queue Settings

Named pass-through scheduling queues, declared as DAG fan-in nodes. A queue is
required when multiple producers feed the same ordinary processing node;
without one, graph validation fails. Queues are coordination points only:
they do not merge row data, join source schemas, or alter token identity.

```yaml
queues:
  inbound:
    description: Fan-in point for orders and refunds   # optional
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | No | Operator-facing description of the queue |

The queue's name is both its DAG node name and the connection name producers
publish to (`on_success: inbound`) and the consumer reads from
(`input: inbound`). Queue names follow the same rules as source names
(lowercase, globally unique, no reserved labels, no leading `__`).

---

## Sink Settings

Named output destinations. At least one required.

```yaml
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/results.csv
      collision_policy: fail_if_exists
      schema:
        mode: observed

  flagged:
    plugin: csv
    on_write_failure: quarantine
    options:
      path: output/flagged.csv
      collision_policy: fail_if_exists
      schema:
        mode: observed

  quarantine:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/quarantine.csv
      collision_policy: fail_if_exists
      schema:
        mode: observed
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plugin` | string | **Yes** | Plugin name: `csv`, `json`, `text`, `aws_s3`, `database`, `azure_blob`, `dataverse`, `chroma_sink` |
| `on_write_failure` | string | **Yes** | Per-row write failure handling: `discard` to drop with audit record, or a sink name to divert to a failsink |
| `options` | object | No | Plugin-specific configuration |

For local file sinks (`csv`, `json`, `text`), `options.collision_policy` can make output-path collisions explicit:

| Policy | Use with | Behavior |
|--------|----------|----------|
| `fail_if_exists` | `mode: write` | Refuse to write if the requested output path already exists |
| `auto_increment` | `mode: write` | Pick a free sibling path such as `results-1.json` |
| `append_or_create` | `mode: append` | Append to an existing output or create it if missing |

### Available Sink Plugins

| Plugin | Purpose |
|--------|---------|
| `csv` | Write to CSV file |
| `json` | Write to JSON file |
| `text` | Write one configured string field per row as canonical LF-delimited text |
| `aws_s3` | Write one bounded cumulative CSV, JSON, or JSONL object to AWS S3 |
| `database` | Write to SQL database |
| `azure_blob` | Write to Azure Blob Storage |
| `dataverse` | Write to Microsoft Dataverse via OData v4 REST API |
| `chroma_sink` | Write to a ChromaDB vector database |

### Text sink contract

The `text` sink requires `path`, `schema`, and `field`. The named field must be
present on every accepted row and its value must already be a string; the sink
does not coerce other values. Embedded CR or LF characters are rejected so each
row remains exactly one record, and every written record ends with canonical LF.
Supported encodings are `utf-8`, `ascii`, `latin-1`, and `cp1252`.

Use `mode: append` with `collision_policy: append_or_create` for append or
resume. Before appending, ELSPETH verifies that existing bytes decode in the
configured encoding, contain no CR separators, and end on an LF record
boundary. The text sink is not eligible as a generic failure sink because it
writes only one selected field and therefore cannot preserve an arbitrary
rejected row losslessly.

### AWS S3 sink

`aws_s3` writes one bounded S3 object per run — the accumulated rows serialized
as CSV, JSON, or JSONL. It has **no credential options**: the client is built
from the ordinary AWS default credential chain, so on ECS you grant the task
role access to the target prefix rather than configuring keys. The sink calls
`HeadObject` before `PutObject` — to reconcile an existing object against the
run's write plan — so the identity needs `s3:GetObject` as well as
`s3:PutObject` on the key it writes.

```yaml
sinks:
  results:
    plugin: aws_s3
    on_write_failure: discard
    options:
      bucket: my-results-bucket
      key: "exports/{{ run_id }}/results.csv"
      format: csv
      overwrite: false
      region_name: ap-southeast-2
      schema:
        mode: observed
```

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `bucket` | string | **Yes** | — | Bucket name or access-point identifier, at most 2048 characters |
| `key` | string | **Yes** | — | Object-key template, rendered once per run |
| `format` | `csv` \| `json` \| `jsonl` | No | `csv` | Serialization of the written object |
| `overwrite` | bool | No | `true` | Allow replacing an existing object at the rendered key |
| `csv_options.delimiter` | string | No | `,` | Single-character field delimiter (`format: csv`) |
| `csv_options.encoding` | string | No | `utf-8` | Output encoding (`format: csv`) |
| `csv_options.include_header` | bool | No | `true` | Write the CSV header record (`format: csv`) |
| `headers` | string or mapping | No | (normalized) | Output header names: normalized (default), `original`, or an explicit mapping |
| `region_name` | string | No | (AWS default resolution) | Signing-region override |
| `endpoint_url` | string | No | (AWS default) | S3-compatible HTTP endpoint. **CLI and batch runs only** — see below |
| `max_object_bytes` | int | No | `268435456` (256 MiB) | Cap on the serialized object; the ceiling is 1 GiB |
| `max_record_chars` | int | No | `1000000` | Cap on characters in one serialized record; the ceiling is 8000000 |

The `key` template is deliberately tiny: literal text plus `{{ run_id }}` and
`{{ timestamp }}`, and nothing else. Any other variable, expression, or
control-flow tag fails configuration with `key template may contain only literal
text and approved variables`. The template may be up to 4096 UTF-8 bytes and the
*rendered* key up to 1024. There is no per-row key — one run writes one object,
so `key` cannot depend on row data.

**Web policy.** Three rules apply to web-authored pipelines, and only the first
is a mere allowlist question:

- The **sink** is authorable from the Web Composer once the operator adds
  `sink:aws_s3` to `plugin_allowlist` — it is not in the required core
  authorization set (see [Web Plugin Policy](#web-plugin-policy)), so an
  unallowlisted deployment simply does not offer it.
- The **source** is prohibited outright and no allowlist entry enables it. An
  `aws_s3` source reads with the server's AWS credential chain while `bucket`
  and `key` are author-controlled, which would let an author read any object the
  server can reach. Use an operator-controlled connector, an allowlisted
  ingestion job, or the batch/CLI runtime for S3 reads.
- `endpoint_url` is forbidden in web-authored options for **both** the source
  and the sink, because a custom storage endpoint redirects server-side requests
  to an author-chosen destination. Omit it and use operator-controlled AWS
  configuration.

---

## Transform Settings

Ordered list of transforms applied to each row. Each transform declares its position in the DAG via `input` (where data comes from) and `on_success` (where successful rows go).

```yaml
transforms:
  # field_mapper renames, selects, and drops fields. It computes nothing.
  - name: enricher
    plugin: field_mapper
    input: source_out
    on_success: naming_in
    on_error: quarantine
    options:
      schema:
        mode: observed
      mapping:
        given_name: first_name
        family_name: last_name
      required_input_fields: [given_name, family_name]  # Validated at DAG construction

  # value_transform computes new or replacement values from expressions.
  - name: compose_full_name
    plugin: value_transform
    input: naming_in
    on_success: output
    on_error: quarantine
    options:
      schema:
        mode: observed
      operations:
        - target: full_name
          expression: "row['first_name'] + ' ' + row['last_name']"
```

The split is not stylistic. `field_mapper` accepts `mapping` (singular),
`select_only`, and `strict` — there is no `computed` key, and because plugin
options are validated with `extra: forbid`, an unknown key is a hard
configuration failure rather than an ignored one. Computing a value is
`value_transform`'s job: an ordered list of `operations`, each with a `target`
field and an `expression` in the
[restricted expression language](#expression-syntax). Operations are applied in
order on a working copy of the row, so a later operation sees the results of the
earlier ones.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique node identifier (human-readable, used in audit trail) |
| `plugin` | string | **Yes** | Plugin name |
| `input` | string | **Yes** | Connection name to receive data from (source `on_success` or another transform's `on_success`) |
| `on_success` | string | **Yes** | Where successful rows go (sink name or connection name for downstream node) |
| `on_error` | string | **Yes** | Sink name for rows that fail processing, or `discard` |
| `options` | object | No | Plugin-specific configuration |
| `options.required_input_fields` | list | No | Fields this transform requires in input (enables DAG validation) |

### Required Input Fields

Transforms can declare which fields they require, enabling the DAG to catch missing field errors at configuration time:

```yaml
transforms:
  - plugin: llm_classifier
    options:
      required_input_fields: [customer_id, message_text]
      # ... other options
```

For template-based transforms (like LLM transforms), use `elspeth.core.templates.extract_jinja2_fields()` to discover which fields your template references:

```python
from elspeth.core.templates import extract_jinja2_fields

template = "Customer {{ row.customer_id }}: {{ row.message_text }}"
fields = extract_jinja2_fields(template)  # frozenset({'customer_id', 'message_text'})
# Add these to required_input_fields in your config
```

### LLM transform (`llm`)

One plugin, `llm`, covers every provider. `provider` selects the variant and
therefore which additional options are legal. Options are validated with
`extra: forbid`, so an option belonging to a different provider is rejected
rather than ignored.

Every variant also takes the ordinary transform options `schema` (**required**)
and `required_input_fields`. `required_input_fields` is not optional in
practice for this plugin: when `prompt_template` — or any query's
`input_fields`/`template` — references a row field that
`required_input_fields` does not declare, configuration fails with
`LLM prompt_template references row fields [...] but options.required_input_fields is not declared`.

Options common to all providers:

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `provider` | `azure` \| `openrouter` \| `bedrock` | **Yes** | — | Provider variant |
| `prompt_template` | string | **Yes** | — | Jinja2 prompt template |
| `model` | string | provider-dependent | — | Model identifier; required for `openrouter` and `bedrock`, defaulted from `deployment_name` for `azure` |
| `system_prompt` | string | No | (none) | Optional system message |
| `temperature` | float | No | `0.0` | Sampling temperature, `0.0`–`2.0`; the default is the deterministic setting |
| `max_tokens` | int | No | (provider default) | Maximum response tokens; must be > 0 |
| `response_field` | string | No | `llm_response` | Row field for the model response; must be a valid Python identifier |
| `queries` | list or mapping | No | (none) | Multi-query specs; omit for single-query mode |
| `lookup` | mapping | No | (none) | Lookup data made available to the template |
| `prompt_template_source` | string | No | (none) | Prompt-template file path recorded for audit; `null` when the template is inline |
| `system_prompt_source` | string | No | (none) | System-prompt file path recorded for audit |
| `lookup_source` | string | No | (none) | Lookup-data file path recorded for audit |
| `pool_size` | int | No | `1` | Concurrent in-flight requests; `1` is sequential and disables pooling |
| `min_dispatch_delay_ms` | int | No | `0` | Floor for the delay between dispatches |
| `max_dispatch_delay_ms` | int | No | `5000` | Ceiling for the delay between dispatches |
| `backoff_multiplier` | float | No | `2.0` | Delay multiplier applied on a capacity error; must be > 1 |
| `recovery_step_ms` | int | No | `50` | Delay reduction applied after a success |
| `max_capacity_retry_seconds` | int | No | `3600` | Per-row ceiling on retrying capacity errors |

`provider: azure` adds:

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `deployment_name` | string | **Yes** | — | Azure OpenAI deployment name; also the default `model` |
| `endpoint` | string | **Yes** | — | Azure OpenAI endpoint URL |
| `api_key` | string | **Yes** | — | Azure OpenAI API key |
| `api_version` | string | No | `2024-10-21` | Azure API version |
| `tracing` | mapping | No | (none) | Optional plugin-internal tracing (`langfuse` or `azure_ai`) |

`provider: bedrock` adds:

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `model` | string | **Yes** | — | LiteLLM Bedrock identifier in `bedrock/<model-id>` form |
| `region_name` | string | No | (AWS default resolution) | AWS signing-region override |
| `tracing` | mapping | No | (none) | Optional plugin-internal tracing (`langfuse` only) |

Bedrock has no credential options at all — it uses the ordinary AWS default
credential chain. See [AWS Bedrock LLM](#aws-bedrock-llm).

`provider: openrouter` adds:

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `model` | string | **Yes** | — | Model identifier such as `anthropic/claude-sonnet-4.6`; must appear in the OpenRouter catalog while `base_url` is the canonical endpoint |
| `api_key` | string | **Yes** | — | OpenRouter API key |
| `base_url` | string | No | `https://openrouter.ai/api/v1` | API base URL; HTTPS is required except for loopback hosts |
| `timeout_seconds` | float | No | `60.0` | Per-request timeout; must be > 0 |
| `tracing` | mapping | No | (none) | Optional plugin-internal tracing (`langfuse` only; `azure_ai` is not supported for OpenRouter) |

```yaml
transforms:
  - name: classify_sentiment
    plugin: llm
    input: classify_in
    on_success: output
    on_error: quarantine
    options:
      provider: openrouter
      model: anthropic/claude-sonnet-4.6
      api_key: ${OPENROUTER_API_KEY}
      prompt_template: "Classify the sentiment of: {{ row.review_text }}"
      response_field: sentiment
      temperature: 0.0
      max_tokens: 200
      required_input_fields: [review_text]
      schema:
        mode: observed
```

Single-query mode writes three row fields: the response itself
(`<response_field>`), plus `<response_field>_usage` (token counts) and
`<response_field>_model` (the model that actually answered).

#### Multi-query (`queries`)

Supplying `queries` runs several questions per row against the one provider
binding. Two authoring forms are accepted: a **mapping** keyed by query name, or
a **list** in which each entry carries its own `name`.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | List form: **Yes**. Mapping form: no — the key supplies it | — | Query identifier; prefixes this query's output fields |
| `input_fields` | mapping | **Yes** | — | Template variable name → row column name |
| `response_format` | `standard` \| `structured` | No | `standard` | `structured` enforces a JSON schema built from `output_fields` |
| `output_fields` | list | No | (none) | Typed structured-output definitions; omit for an unstructured response |
| `template` | string | No | (the node's `prompt_template`) | Per-query Jinja2 template override |
| `max_tokens` | int | No | (the node's `max_tokens`) | Per-query override |

Each `output_fields` entry:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `suffix` | string | **Yes** | Column suffix; the output column is `<query_name>_<suffix>` |
| `type` | `string` \| `integer` \| `number` \| `boolean` \| `enum` | **Yes** | Declared type, enforced against the response |
| `values` | list of strings | For `enum` only | Allowed values; required for `enum` and rejected for every other type |

A per-query `template` may not contain `{{interpretation:...}}` tokens —
interpretation review rewrites only the node-level `prompt_template`, so a token
here would survive to the run and fail as a Jinja error. Put reviewed slots in
the node-level template.

Multi-query mode emits **suffixed fields only** — there is no unprefixed base
field. Each query contributes `<query_name>_<response_field>` plus
`<query_name>_<response_field>_usage` and `<query_name>_<response_field>_model`,
and each structured output field lands at `<query_name>_<suffix>`. Suffixes may
not collide across queries, and `usage`, `model`, and `error` are reserved.

```yaml
transforms:
  - name: assess_review
    plugin: llm
    input: assess_in
    on_success: output
    on_error: quarantine
    options:
      provider: openrouter
      model: anthropic/claude-sonnet-4.6
      api_key: ${OPENROUTER_API_KEY}
      prompt_template: "Assess this review: {{ review }}"
      max_capacity_retry_seconds: 30
      required_input_fields: [review_text]
      schema:
        mode: observed
      queries:
        - name: quality
          input_fields:
            review: review_text
          response_format: structured
          output_fields:
            - suffix: score
              type: integer
            - suffix: band
              type: enum
              values: [low, medium, high]
            - suffix: rationale
              type: string
        - name: topic
          input_fields:
            review: review_text
          template: "Name the single main topic of: {{ review }}"
          max_tokens: 40
```

That node writes `quality_score`, `quality_band`, `quality_rationale`, and
`quality_llm_response` for the first query and `topic_llm_response` for the
second, plus the per-query metadata pair for each —
`quality_llm_response_usage`, `quality_llm_response_model`,
`topic_llm_response_usage`, and `topic_llm_response_model`.

#### Web-authored LLM nodes

Three of these options are constrained further on the Web Composer surface,
beyond whatever the plugin itself accepts:

| Option | Web policy |
|--------|------------|
| `base_url` | **Forbidden.** The `api_key` is resolved server-side, so an author-chosen base URL would direct a server-held bearer token to a destination the author picked — a credential-egress/SSRF path. Omit it to use the canonical OpenRouter endpoint; a private OpenAI-compatible gateway is an operator-controlled runtime concern. |
| `tracing` | **Forbidden.** Tracing can send server-held credentials and pipeline inputs or outputs to a configured destination. |
| `max_capacity_retry_seconds` | Must be set explicitly to 30 or less for a **sequential** (`pool_size: 1`) multi-query node. The plugin default of one hour can monopolize the web execution worker; use `pool_size` greater than 1 for pooled retry handling instead. |

The plugin itself tolerates an HTTP loopback `base_url` so the shipped local-dev
examples run under the CLI. That single-machine threat model does not hold for a
hosted server, which is why the web boundary pins `base_url` separately.

### Available Transform Plugins

| Plugin | Purpose |
|--------|---------|
| `azure_document_intelligence` | Enrich rows with Azure AI Document Intelligence extraction |
| `blob_fetch` | Fetch an operator-authorised remote document into the run payload store |
| `blob_csv_expand` | Expand a payload-store CSV blob into output rows |
| `passthrough` | Pass rows unchanged |
| `field_mapper` | Rename, select, and drop fields (renaming only — it computes nothing) |
| `value_transform` | Compute new or replacement field values from expressions |
| `truncate` | Limit string field lengths |
| `keyword_filter` | Filter rows by regex patterns |
| `json_explode` | Expand list-valued fields to multiple rows |
| `batch_stats` | Compute statistics over a batch, optionally one row per `group_by` value |
| `batch_replicate` | Batch deaggregation; configure as an aggregation with `output_mode: transform` |
| `batch_distribution_profile` | Numeric-only batch distribution statistics; use `batch_top_k` for categorical counts/frequencies |
| `report_assemble` | Assemble a batch of text rows into one report/text row with pagination metadata |
| `web_scrape` | HTML content extraction with SSRF prevention |
| `llm` | Unified LLM transform (azure/openrouter/bedrock providers, single/multi-query) |
| `azure_content_safety` | Detect harmful content via Azure AI |
| `azure_prompt_shield` | Detect prompt injection via Azure AI |
| `aws_bedrock_content_safety` | Detect configured harmful-content categories through Bedrock Guardrails |
| `aws_bedrock_prompt_shield` | Detect prompt attacks through Bedrock Guardrails |
| `aws_textract_document_analysis` | Extract text, tables, forms, and layout from S3-hosted documents through Amazon Textract |
| `rag_retrieval` | Enriches rows with retrieval-augmented context from search providers |

### AWS Bedrock LLM

Bedrock uses LiteLLM with the ordinary AWS default credential chain. On ECS,
grant Bedrock permissions to the task role; do not put access keys in plugin
configuration.

```yaml
transforms:
  - name: classify_with_bedrock
    plugin: llm
    input: classify_in
    on_success: output
    on_error: discard
    options:
      provider: bedrock
      model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
      region_name: ap-southeast-2
      prompt_template: "Classify: {{ row.text }}"
      required_input_fields: [text]
      schema:
        mode: observed
```

### Custom OpenAI-compatible endpoints

The `llm` transform's OpenRouter provider is a plain OpenAI Chat Completions
client. Overriding its `base_url` points it at any other endpoint that speaks the
same shape — a self-hosted proxy, an agency translation layer, an ELSPETH LLM
compatibility gateway, or a local development server. No gateway-specific
option is involved.

```yaml
transforms:
  - name: classify
    plugin: llm
    input: classify_in
    on_success: output
    on_error: discard
    options:
      provider: openrouter
      base_url: https://gateway.internal.example.gov.au/v1
      api_key: ${LLM_GATEWAY_BEARER}
      model: openai/gpt-4o
      prompt_template: "Classify: {{ row.text }}"
      required_input_fields: [text]
      schema:
        mode: observed
```

`base_url` must be HTTPS, carry no embedded user information, and default to
`https://openrouter.ai/api/v1` when omitted. HTTP is permitted only for a
loopback host, so the shipped local ChaosLLM example
(`http://127.0.0.1:8199/v1`) validates; the bearer never leaves the machine
in that case.

The `model` value is interpreted by whichever endpoint you configured. When
`base_url` is left at the OpenRouter default, `model` is checked against the
OpenRouter catalogue; when you override `base_url`, that check is dropped,
because the endpoint — not OpenRouter — owns the model identifiers.

### The `gateway` provider

`provider: gateway` targets the ELSPETH LLM compatibility gateway
specifically. It adds startup capability preflight and exact envelope error
codes on top of what `base_url` pointing gives you.

```yaml
transforms:
  - name: classify
    plugin: llm
    input: classify_in
    on_success: output
    on_error: discard
    options:
      provider: gateway
      endpoint: https://gateway.internal.example.gov.au/v1
      api_key: ${LLM_GATEWAY_BEARER}
      model: standard
      contract_major: 1
      required_capabilities: [text, json_schema, usage]
      prompt_template: "Classify: {{ row.text }}"
      required_input_fields: [text]
      schema:
        mode: observed
```

| Option | Notes |
|--------|-------|
| `endpoint` | Gateway base URL; must end with `/v1`. HTTPS, or HTTP against the literal `127.0.0.1` loopback host only. No query string or fragment. |
| `api_key` | Static inbound bearer the gateway expects. |
| `model` | A **logical alias** the gateway resolves server-side, not a raw upstream model id. There is no local catalogue to check it against; the gateway's readiness document is the authority. |
| `contract_major` | Wire contract major version this configuration expects. Only `1` is supported. |
| `required_capabilities` | Closed set: `text`, `tools`, `json_object`, `json_schema`, `seed`, `usage`. Checked at startup against what the gateway declares. |

At startup the provider reads `/readyz` and verifies the contract major, the
adapter identity, the model alias, and every required capability, then runs
one bounded real completion — a readiness document alone is not accepted as
health. A gateway whose adapter cannot do JSON Schema therefore fails the run
at boot rather than per row. Gateway error codes (`invalid_request`,
`contract_mismatch`, `model_not_allowed`, `capability_unsupported`,
`upstream_unauthorized`, and the rest) map to definite retryable or
non-retryable outcomes instead of being inferred from an HTTP status.

Before pointing a pipeline at any endpoint you do not operate, read
[Custom LLM Endpoints](environment-variables.md#custom-llm-endpoints) — it
sets out what ELSPETH can and cannot tell you about the endpoint you chose.

### AWS Bedrock Guardrail shields

CLI and batch pipelines use an explicit trained-operator binding. The Guardrail
identifier, immutable numeric version, region, fields, and schema are required;
there are no access-key or endpoint options. Prompt shielding always evaluates
content as `INPUT`. Content safety accepts `source: INPUT` for inbound text or
`source: OUTPUT` for generated text.

```yaml
transforms:
  - name: screen_prompt
    plugin: aws_bedrock_prompt_shield
    input: prompt_in
    on_success: generation_in
    on_error: blocked
    options:
      guardrail_identifier: operatorpromptguardrail
      guardrail_version: "7"
      region: ap-southeast-2
      fields: [prompt]
      schema:
        mode: observed

  - name: screen_generated_text
    plugin: aws_bedrock_content_safety
    input: generated_in
    on_success: output
    on_error: blocked
    options:
      guardrail_identifier: operatorcontentguardrail
      guardrail_version: "4"
      region: ap-southeast-2
      fields: [response]
      source: OUTPUT
      schema:
        mode: observed
```

Each configured field must receive an explicit safe result. A detect-only
positive is blocked even when the top-level Guardrail action is `NONE`, and an
intervention, service failure, or malformed response also fails closed without
including provider-generated text in row data or error details.

The harmful-content categories required by this AWS plugin do not include
Azure Content Safety's `self_harm` category. A deployment must not claim Azure
coverage parity unless an additional approved control covers that category.

When a control mode is `required`, these shields must cover every stream an
`llm` node emits — including its `on_error` path. See
[Required controls and error routing](#required-controls-and-error-routing) for
the two conforming shapes and what each does to a failed row.

### AWS Textract document analysis

`aws_textract_document_analysis` runs Amazon Textract's asynchronous
document-analysis job over a document already stored in S3, and adds the
extracted text, tables, forms, and layout to the row.

Unlike most transforms, it takes its S3 references **from row data, not from
static options**: `bucket_field` and `key_field` name the input fields holding
the bucket and key, so one manifest row drives one document. `version_field` is
optional and pins a specific S3 object version.

Because those references come from row data, the manifest source must **declare
the fields as a fixed schema**. `required_input_fields` is a graph-validated
contract: against a dynamic (`mode: observed`) producer the graph fails to build
with `Schema contract violation … Producer (csv) guarantees: (none - dynamic
schema)`. This complete example runs as written:

```yaml
sources:
  manifest:
    plugin: csv
    on_success: extract_in
    options:
      path: /app/input/manifest.csv
      on_validation_failure: discard   # required on the csv source
      schema:
        mode: fixed                    # required: proves doc_bucket/doc_key exist
        fields:
          - "doc_id: str"
          - "doc_bucket: str"
          - "doc_key: str"

sinks:
  extracted:
    plugin: json
    on_write_failure: discard
    options:
      path: /app/output/extracted.json
      schema:
        mode: observed

transforms:
  - name: extract_invoice
    plugin: aws_textract_document_analysis
    input: extract_in
    on_success: extracted
    on_error: discard
    options:
      region: ap-southeast-2
      bucket_field: doc_bucket
      key_field: doc_key
      feature_types: [TABLES, FORMS]
      text_field: document_text
      page_count_field: page_count
      required_input_fields: [doc_bucket, doc_key]
      schema:
        mode: observed
```

with a manifest of the shape:

```csv
doc_id,doc_bucket,doc_key
1,my-documents-bucket,invoices/2026-07/invoice-001.pdf
```

`feature_types` is a unique selection from `TABLES`, `FORMS`, `QUERIES`,
`SIGNATURES`, and `LAYOUT`.

**`QUERIES` and `queries` are bound together in both directions.** The
constraint is not one-way: `QUERIES` in `feature_types` without a `queries`
array is rejected, and a `queries` array without `QUERIES` in `feature_types`
is rejected too — both with `queries and the QUERIES feature must be configured
together`. Add or remove the pair as a unit.

Each `queries` element is a question put to every document:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | **Yes** | The question, 1–200 characters |
| `alias` | string | No | Short label for the answer, 1–200 characters |
| `pages` | list of strings | No | Page selectors; empty (the default) means Textract's own default page selection |

`text` and `alias` are restricted to the character set Amazon Textract accepts
for query text (ASCII letters, digits, whitespace, and common punctuation); a
value outside it is rejected at configuration time, not at call time.

Each `pages` selector is 1–9 characters matching `^[0-9*-]+$` and is one of a
single positive page number (`"3"`), a closed range (`"1-3"`, ordered, both
endpoints positive), an open-ended range (`"2-*"`), or `"*"` for every page.
`"*"` must be the only selector, and duplicate selectors are rejected.

```yaml
      feature_types: [TABLES, FORMS, QUERIES]
      queries:
        - text: What is the invoice total?
          alias: invoice_total
          pages: ["1"]
        - text: What is the payment due date?
          alias: due_date
          pages: ["1-3"]
        - text: Who is the vendor?          # pages omitted
```

Query answers arrive as the normalized `queries` facet. Land them on a row field
with `extract: {queries: <field_name>}`, or read them out of the bounded
aggregate in `result_field`.

Choose at least one output field or the extraction has nowhere to land:
`text_field` (page-ordered `LINE` text), `page_count_field`,
`metadata_field` (bounded job metadata), `result_field` (the bounded
provider-shaped aggregate), or `extract` (mappings from normalized document
facets to row fields).

**Credentials.** `auth_mode` defaults to `default_chain`, which uses the
ordinary AWS credential chain — on ECS, the task role. `secret_refs` mode
exists for CLI and batch pipelines that resolve credentials from ELSPETH secret
references; web-authored pipelines must not carry access keys.

**IAM.** The calling identity needs `textract:StartDocumentAnalysis` and
`textract:GetDocumentAnalysis`. Neither action names an ARN, so `"*"` is the
only expressible resource — the effective scope is the S3 object grant, because
Textract reads `DocumentLocation.S3Object` under the caller's own credentials
and can therefore only analyse documents the identity could already read. The
AWS ECS scenario module grants both actions to the task role and carries the
same statement in the permissions boundary.

**The S3 object must fall inside the caller's own object grant, and a document
outside it fails misleadingly.** The effective read scope is whatever
`s3:GetObject` grant the pipeline identity holds. When `version_field` is
configured, the identity also needs `s3:GetObjectVersion` on that same object
scope; `s3:GetObject` alone does not authorize the version-pinned read. In the
reference AWS ECS deployment both actions are granted on
`<bucket>/<namespace>/<run_id>/*`, so documents must live under that prefix — a
document elsewhere in the *same* bucket is unreadable. Textract reports that as
`{"code": "InvalidS3ObjectException", "error_type": "service_error", "reason":
"submit_failed"}`; the provider does not distinguish an authorization miss from
a missing or corrupt file. ELSPETH therefore classifies this code with
`cause: s3_object_unreadable` and an `error` hint in the audit reason: check
the role's `s3:GetObject` scope and, for a version-pinned row,
`s3:GetObjectVersion` (then object existence and region) before suspecting the
document itself. Read the granted prefix from the task-role policy rather than
guessing:

```sh
aws iam get-role-policy --role-name <task-role> --policy-name <task-policy> \
  --query 'PolicyDocument.Statement[?Sid==`UseAcceptanceObjects`].Resource' --output json
```

To tell an authorization problem from a bad document, submit the identical
object with a principal you know can read it
(`aws textract start-document-analysis --document-location ...`). If that
succeeds, the file is fine and the pipeline identity's object grant is the
problem.

**Bounds and polling.** Each document is capped by `max_result_pages`,
`max_blocks`, and `max_result_bytes`, and the job poll is governed by
`poll_interval_seconds`, `poll_backoff_multiplier`, `poll_max_interval_seconds`,
`poll_timeout_seconds`, and `batch_wait_timeout_seconds`. The defaults suit
ordinary business documents; raise the caps deliberately, because they bound
how much provider-shaped data can enter one row.

**Extracted text is untrusted third-party content.** A document supplied by an
outside party can carry text crafted to steer a downstream model, exactly like
a scraped web page. Put a prompt shield between this transform and any `llm`
node that consumes its output. When `ELSPETH_WEB__PLUGIN_CONTROL_MODES` sets
`prompt_shield` to `required`, web-authored pipelines cannot omit that shield;
under `recommend` it is the author's responsibility.

### Required controls and error routing

When a control mode is `required`, the web surface checks that the control
*dominates* (for `prompt_shield`) or *post-dominates* (for `content_safety`)
every `llm` node. The check treats **each stream a node emits as an independent
path**: `on_success`, `on_error`, every gate route, and every fork branch.
There is one exemption — the virtual `discard` target, which produces no
stream.

**With `content_safety: required`, an `llm` node's `on_error` must be
`discard`.** Two independent rules combine to leave exactly one option:

- A transform's `on_error` is a **sink name or `discard`** — never a connection
  name. Naming a stream there fails graph construction with
  `No producer for connection '<name>'`, because only `on_success` publishes a
  connection. So no transform, including a control, can be interposed on an
  error path.
- Any sink target on that path fails the coverage check with
  `output_error_route_not_post_dominated`, because the row reaches a sink
  without passing the control.

`discard` is therefore the only conforming value, and it is what the check
accepts:

```yaml
transforms:
  - name: summarize_document
    plugin: llm
    input: summarize_in
    on_success: screen_summary_in
    on_error: discard                 # the only value that satisfies the check
    options:
      provider: bedrock
      model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
      region_name: ap-southeast-2
      prompt_template: "Summarise: {{ row.document_text }}"
      required_input_fields: [document_text]
      response_field: summary
      schema:
        mode: observed

  - name: screen_summary
    plugin: aws_bedrock_content_safety
    input: screen_summary_in
    on_success: output
    on_error: quarantine
    options:
      guardrail_identifier: operatorcontentguardrail
      guardrail_version: "4"
      region: ap-southeast-2
      fields: [summary]               # must cover the llm response_field
      source: OUTPUT
      schema:
        mode: observed
```

Note that the success-path control must scan the `llm` node's `response_field`:
coverage requires the control's `fields` to include every protected field, so
`response_field: summary` pairs with `fields: [summary]`.

What `discard` costs you is the row's **content**, not the record of it. The
failed row reaches no sink, so there is nothing to inspect or re-drive later; the
audit trail still records the terminal outcome for that row and its content hash,
so the failure remains explainable, attributable to the node, and countable in
run accounting.

If a deployment genuinely needs failed LLM rows preserved in a quarantine sink,
that is an operator decision rather than an authoring workaround. `on_error:
<quarantine sink>` builds and runs perfectly well; it is only the *required*
control mode that rejects it. Either the operator sets `content_safety` to
`recommend` in `ELSPETH_WEB__PLUGIN_CONTROL_MODES` — making the control the
author's responsibility, as it is by default — or the pipeline runs under the
CLI/batch runtime, where the web plugin policy does not apply.

On the success path, intermediate transforms between the `llm` node and the
control are allowed — the check keeps walking downstream until it finds one —
but every stream that intermediate node emits must itself reach a control or
`discard`, and a `field_mapper` that renames a protected field is followed
through while one that drops it (`select_only: true` without that field) fails
the check.

The input side has the mirror-image trap: a node between the prompt shield and
the `llm` node that writes a shielded field replaces scanned content with
unscanned content, so the shield no longer covers it. Coverage fails closed
there on any node whose write set it cannot prove — only `passthrough`,
`value_transform`, and `field_mapper` have provable write sets.

---

## Gate Settings

Config-driven routing based on expressions. Gates evaluate conditions and route rows to sinks or forward them.

```yaml
gates:
  - name: quality_check
    input: enriched
    condition: "row['confidence'] >= 0.85"
    routes:
      "true": next_step_in   # Connection name for downstream node
      "false": discard       # Virtual terminal discard; records gate_discarded

  - name: amount_threshold
    input: validated
    condition: "row['amount'] > 1000"
    routes:
      "true": high_values
      "false": output
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique gate identifier |
| `input` | string | **Yes** | Connection name to receive data from |
| `condition` | string | **Yes** | Expression to evaluate (see [Expression Syntax](#expression-syntax)) |
| `routes` | object | **Yes** | Maps evaluation results to destinations |
| `fork_to` | list | No | Branch paths for fork operations |

### Route Destinations

| Destination | Behavior |
|-------------|----------|
| `<connection_name>` | Forward to a downstream node that declares this as its `input` |
| `<sink_name>` | Route directly to named sink |
| `fork` | Split to multiple paths (requires `fork_to`) |
| `discard` | Stop this branch without a sink; records an audited `gate_discarded` terminal outcome |

All route destinations must be explicit connection names, sink names, `fork`, or the virtual `discard` target. There is no implicit "forward to next step" — every routing decision must name its destination.

### Boolean Conditions

Boolean expressions (comparisons, `and`/`or`) must use `"true"`/`"false"` as route labels:

```yaml
# CORRECT - boolean condition uses true/false
gates:
  - name: threshold
    condition: "row['amount'] > 1000"
    routes:
      "true": high_values
      "false": output

# WRONG - boolean condition with non-boolean labels
gates:
  - name: threshold
    condition: "row['amount'] > 1000"
    routes:
      "above": high_values  # ERROR: condition returns True/False, not "above"
      "below": output
```

### Fork Operations

Split rows to multiple parallel paths:

```yaml
gates:
  - name: parallel_analysis
    condition: "True"
    routes:
      "true": fork
    fork_to:
      - sentiment_path
      - entity_path
```

---

## Aggregation Settings

Batch rows until a trigger fires, then process as a group.

```yaml
aggregations:
  - name: batch_stats
    plugin: batch_stats
    input: enriched
    on_success: output
    on_error: discard           # Sink name for batch errors, or 'discard'
    trigger:
      count: 100              # Fire after 100 rows
      timeout_seconds: 3600   # Or after 1 hour
    output_mode: transform
    expected_output_count: 1  # Optional; omit when group_by can emit multiple rows
    options:
      schema:
        mode: observed
      value_field: amount
      group_by: customer_tier  # Optional: one aggregate row per tier
      compute_mean: true
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique aggregation identifier |
| `plugin` | string | **Yes** | Aggregation plugin name |
| `input` | string | **Yes** | Connection name to receive data from |
| `on_success` | string | No | Where successful output rows go (sink name or connection name) |
| `on_error` | string | **Yes** | Sink name for rows that fail batch processing, or `discard` |
| `trigger` | object | **Yes** | When to flush the batch |
| `output_mode` | string | No | `passthrough` or `transform` (default: `transform`) |
| `expected_output_count` | int | No | For `transform` mode: validate output row count |
| `options` | object | No | Plugin-specific configuration |

The `report_assemble` aggregation collates a batch of text rows into a single report row with pagination metadata sourced from the batch context:

```yaml
aggregations:
  - name: pages
    plugin: report_assemble
    input: lines
    on_success: output
    on_error: discard
    trigger:
      count: 80
    output_mode: transform
    expected_output_count: 1
    options:
      schema:
        mode: observed
      text_field: line
      format: markdown
      title: "Run report"
```

Omit `trigger` to emit a single report covering all source rows at end-of-source.

### Trigger Configuration

At least one trigger type is required:

| Trigger | Type | Description |
|---------|------|-------------|
| `count` | int | Fire after N rows accumulated |
| `timeout_seconds` | float | Fire after N seconds since first accept |
| `condition` | string | Fire when expression evaluates to true |

Multiple triggers can be combined (first to fire wins):

```yaml
trigger:
  count: 1000
  timeout_seconds: 3600
  condition: "row['batch_count'] >= 500 and row['batch_age_seconds'] < 30"
```

**Important:** Trigger conditions operate at the **batch level**, not the row level. Only `batch_count` and `batch_age_seconds` are available as row keys. For row-level routing decisions, use Gates instead.

**Note:** End-of-source is always checked implicitly and doesn't need configuration.

### Output Modes

| Mode | Behavior |
|------|----------|
| `transform` | Batch applies transform function to produce results (default) |
| `passthrough` | Batch releases all accepted rows unchanged |

For N→1 aggregation (e.g., computing statistics), use `transform` mode with `expected_output_count: 1` to validate cardinality.

---

## Coalesce Settings

Merge tokens from parallel fork paths back into a single token.

```yaml
coalesce:
  - name: merge_analysis
    branches:
      - sentiment_path
      - entity_path
    policy: require_all
    merge: union

  - name: quorum_merge
    branches:
      - fast_model
      - slow_model
      - fallback_model
    policy: quorum
    quorum_count: 2
    merge: nested
    timeout_seconds: 30
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique coalesce identifier |
| `branches` | list or dict | **Yes** | Branch names to wait for (min 2). List form `[a, b]` is shorthand for `{a: a, b: b}`. Dict form maps branch identity → input connection. |
| `on_success` | string | No | Sink name or connection name for coalesce output (required when coalesce is terminal) |
| `policy` | string | No | How to handle partial arrivals (default: `require_all`) |
| `merge` | string | No | How to combine data (default: `union`) |
| `union_collision_policy` | string | No | Field-level collision resolution for `merge: union` (default: `last_wins`) |
| `timeout_seconds` | float | No | Max wait time |
| `quorum_count` | int | No | Minimum branches required (for `quorum` policy) |
| `select_branch` | string | No | Which branch to take (for `select` merge) |

### Policies

| Policy | Behavior | Requirements |
|--------|----------|--------------|
| `require_all` | Wait for all branches | - |
| `quorum` | Wait for N branches | `quorum_count` required |
| `best_effort` | Wait until timeout, use what arrived | `timeout_seconds` required |
| `first` | Use first branch to arrive | - |

### Merge Strategies

| Strategy | Behavior | Requirements |
|----------|----------|--------------|
| `union` | Combine all fields from all branches | - |
| `nested` | Each branch's data nested under branch name | - |
| `select` | Take data from one specific branch | `select_branch` required |

### Union Collision Policy

When `merge: union` is used and two or more branches emit the same field name, `union_collision_policy` controls how the field-level conflict is resolved. This is **only meaningful for `merge: union`** — it is ignored for `nested` and `select`.

| Value | Behavior |
|-------|----------|
| `last_wins` *(default)* | The last branch in declaration order wins. Matches the historical behavior of union merges. |
| `first_wins` | The first branch in declaration order wins. |
| `fail` | Raise `CoalesceCollisionError` the moment any field collides. No merged row is produced. The full collision record (origin of every field plus each contributing `(branch, value)` pair) is still written to the audit trail on the failed node state. |

> **Note on `fail`:** Collision detection is **name-based**, not value-based. Two branches that both emit a field called `id` with the *same* value still trigger `fail` — the executor does not compare values to decide whether the overlap is "real." If your branches share trivially-identical fields (like an `id` carried unchanged through both transforms), use `last_wins` or `first_wins` instead, or rename the shared fields out of one branch.

Regardless of the chosen value, every union merge records:

- `union_field_origins` — a mapping from every merged field to the branch that produced the winning value. Always populated so auditors can reconstruct field-level provenance even when no collision occurred.
- `union_field_collision_values` — a mapping from each colliding field to the ordered list of `(branch, value)` pairs. Populated only when at least one field collided.

`union_collision_policy` is **orthogonal to `policy`**: `policy` governs branch-level arrival (what to do when some branches never arrive), while `union_collision_policy` governs field-level conflict within an already-assembled merge. They are independent axes and can be combined freely.

```yaml
coalesce:
  - name: strict_merge
    branches:
      - sentiment_path
      - entity_path
    policy: require_all          # branch-level arrival policy
    merge: union
    union_collision_policy: fail  # field-level collision policy — abort on overlap
    on_success: output
```

---

## Pipeline Dependencies

Declare pipelines that must run before this one. Used for multi-pipeline workflows like RAG ingestion (index first, then query).

```yaml
depends_on:
  - name: indexing
    settings: pipelines/index_pipeline.yaml
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique label for this dependency |
| `settings` | string | **Yes** | Path to the dependency pipeline settings file |

Dependencies are executed in order before the main pipeline starts. Each dependency produces a `DependencyRunResult` recorded in the audit trail.

---

## Commencement Gates

Go/no-go conditions evaluated after dependencies complete but before the main pipeline starts. Use these for pre-flight checks (e.g., verifying a vector store is populated).

```yaml
commencement_gates:
  - name: collection_ready
    condition: "collections['products']['count'] > 0"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | Unique label for this gate |
| `condition` | string | **Yes** | Expression evaluated against pre-flight context (see the bound names below) |

Conditions use the same restricted grammar as gates and aggregation triggers
(see [Expression Syntax](#expression-syntax)), but they run before any row
exists, so `row` is **not** bound. Exactly three names are available:

| Name | Shape | Description |
|------|-------|-------------|
| `collections` | `{<collection>: {reachable, count}}` | One entry per [collection probe](#collection-probes) |
| `dependency_runs` | `{<name>: {run_id, settings_hash, duration_ms, indexed_at}}` | One entry per completed [pipeline dependency](#pipeline-dependencies) |
| `env` | `{<key>: <value>}` | Explicitly supplied non-secret gate environment values |

Because attribute access is forbidden by the grammar, every lookup is a
subscript or a single-argument `.get()`:
`collections['products']['count']`, not `collections['products'].count`.
Nested values are read the same way — `dependency_runs['indexing']['run_id']`.

Gate failures raise `CommencementGateFailedError` and abort the pipeline. Gate
passes are recorded in the audit trail; the audited context snapshot includes
`collections` and `dependency_runs` in full but records only the *key names*
from `env`, never its values.

---

## Collection Probes

Vector store readiness checks that run after dependencies and populate context for commencement gates.

```yaml
collection_probes:
  - collection: products
    provider: chroma
    provider_config:
      persist_directory: ./chroma_data
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `collection` | string | **Yes** | Collection name to probe |
| `provider` | string | **Yes** | Provider type (e.g., `chroma`) |
| `provider_config` | object | No | Provider-specific connection configuration |

Probe results are available to commencement-gate expressions as
`collections['<collection_name>']`, a mapping with two keys:

| Key | Type | Meaning |
|-----|------|---------|
| `reachable` | bool | Whether the probe reached the provider |
| `count` | int | Documents the probe observed in the collection |

A collection that was never probed is absent from `collections`, and
subscripting it fails the gate with `Field '<name>' not found in dict` rather
than reading as zero. Use `collections.get('<name>') is not None` when the probe
itself is conditional.

---

## Landscape Settings (Audit Trail)

Configure the audit trail database and optional change journal.

```yaml
landscape:
  enabled: true
  backend: sqlite
  url: sqlite:///./runs/audit.db
  export:
    enabled: true
    sink: audit_archive
    format: json
    signing_mode: hmac_sha256
    signer_key_id: audit-export-2026-q3-v1
    signing_secret_ref: ELSPETH_AUDIT_EXPORT_SIGNING_KEY
    signer_rotation_policy: multi_version
    total_record_limit: 1000000
    total_byte_limit: 1073741824
    chunk_limit: 100
    per_chunk_record_limit: 10000
    per_chunk_byte_limit: 16777216
    spool_root: .elspeth/audit-export-spool/primary
    content_store:
      content_store_id: audit-archive-v1
      namespace: audit-exports
      root: .elspeth/audit-export-content-store/primary
      policy_version: retention-v1
      retention_days: 2555
      durability: replicated
  # Optional: JSONL change journal for emergency backup
  dump_to_jsonl: false
  dump_to_jsonl_path: audit.journal.jsonl
  dump_to_jsonl_include_payloads: false
```

For SQLite, ELSPETH resolves a relative journal path against the directory
containing `landscape.url`. Here, both files live under `./runs/`.

Committed journal batches are bound to the canonical sidecar path that
created them. Startup recovery only drains that path's backlog; changing a
worker's journal path does not move or acknowledge batches owned by the old
path. Reopen the database with the original path to recover that backlog.
Concurrent drains for one path are serialized across processes.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable audit trail recording |
| `backend` | string | `sqlite` | Database backend: `sqlite`, `sqlcipher`, `postgresql` |
| `encryption_key_env` | string | `ELSPETH_AUDIT_KEY` | Environment variable holding the SQLCipher passphrase (`backend: sqlcipher` only) |
| `url` | string | `sqlite:///./state/audit.db` | SQLAlchemy database URL |
| `export` | object | (disabled) | Post-run audit export configuration |
| `dump_to_jsonl` | bool | `false` | Write append-only JSONL change journal |
| `dump_to_jsonl_path` | string | (derived from url) | Path for JSONL journal file; SQLite resolves relative paths against the database directory |
| `dump_to_jsonl_fail_on_error` | bool | `false` | Fail database startup if a committed outbox batch cannot be published during recovery |
| `dump_to_jsonl_include_payloads` | bool | `false` | Include request/response bodies in journal |
| `dump_to_jsonl_payload_base_path` | string | (from payload_store) | Payload store path for inlining |

### Landscape schema epoch 30

Landscape epoch 26 added durable sink-effect streams, effects, ordered members,
attempts, and sealed audit-export snapshots. Epoch 27 adds durable coalesce
effects and normalized parent evidence so materialization and completion can be
replayed safely after a crash. Epoch 28 moves primary-effect provenance onto
each failsink effect member so recovered batches spanning more than one primary
effect remain exactly attributable. Epoch 29 adds run-scoped token ancestry and
validation-error links, canonical node output-contract hashes, durable batch
expansion claims, and a transaction-owned sidecar-journal outbox. Each outbox
batch records its
canonical sidecar owner so another worker or path cannot publish or acknowledge
it. Epoch 30 adds `token_work_items.row_union_name` so a recovered scheduler can
attribute a blocked work item to its declared row_union barrier. See the
[sink-effect recovery runbook](../runbooks/sink-effect-recovery.md).

ELSPETH is pre-1.0. It does not transform an older Landscape schema into epoch
30, either automatically at startup or through an operator migration command.
Stop and uninstall the old deployment, archive or export evidence when policy
requires it, delete/recreate the Landscape database, then reinstall and
initialize this ELSPETH version. PostgreSQL schema-owner and runtime/DML roles
remain separate; recreation is an operator action. Code that understands only
an older epoch must not be rolled back over an epoch-30 database.

Data-preserving, version-to-version schema migrations become a first-class
compatibility obligation at 1.0. They are intentionally not a pre-1.0 promise.

#### Historical epoch-25 artifact identity

Landscape epoch 25 makes artifact logical-effect identity structural. Fresh
SQLite and PostgreSQL schemas carry a partial unique index on
`artifacts(run_id, idempotency_key)` for rows whose idempotency key is non-null.

An older SQLite or PostgreSQL Landscape schema missing this index is stale and
must be recreated under the pre-1.0 policy above. Read-only and inspection-only
opens never alter schema. Code that only understands epoch 23 or 24 must not be
rolled back over an epoch-25-or-newer database.

#### Historical epoch-24 token ownership

Landscape epoch 24 makes the persisted row authoritative for token run
ownership. Fresh SQLite and PostgreSQL schemas enforce
`tokens(row_id, run_id) -> rows(row_id, run_id)` in addition to the existing
single-column references.

An older SQLite or PostgreSQL Landscape schema missing this constraint is stale
and must be recreated under the pre-1.0 policy above. Code that only
understands epoch 23 must not be rolled back over an epoch-24-or-newer
database.

#### Historical epoch-23 boundary

Landscape epoch 23 adds `run_web_plugin_policy`, an optional one-to-one audit
row for each web-initiated run. The row records the immutable policy and
request-snapshot hashes, authorized and available kind-qualified plugin IDs,
control modes, selected implementations, safe profile aliases, plugin code
identities, the binding-generation fingerprint, and bounded decision codes.
It never records a principal ID, secret or secret-reference value, private
profile binding, or remote response payload. CLI runs correctly have no row.

Epoch 23 is a deliberate pre-1.0 one-way compatibility boundary for SQLite
and PostgreSQL. ELSPETH does not add this table to an existing pre-23
Landscape database. A database owner must obtain archive/export approval where
retention applies, stop writers, drop/recreate the Landscape schema, and run
the fresh schema-owner initialization path. Runtime/DML roles must not receive
DDL. Rolling code back to epoch-22 code after an epoch-23 recreation is unsafe;
deployment and rollback decisions must cite an approved release/schema
compatibility record rather than relying on a structural probe alone.

### Export Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable audit trail export after run |
| `sink` | string | - | Sink name to export to (required when enabled) |
| `format` | string | `csv` | Export format: `csv`, `json` |
| `signing_mode` | string | `unsigned` | `unsigned` or `hmac_sha256` |
| `signer_key_id` | string | `UNSIGNED` | Credential-free public signer key ID/version recorded in snapshot identity |
| `signing_secret_ref` | string | - | Exact environment-variable name containing the HMAC key; required for `hmac_sha256` |
| `signer_rotation_policy` | string | `multi_version` | `multi_version` allows a new signer identity for a new snapshot; `single_export` refuses a different signer identity for the same export lineage |
| `exporter_version` | string | `landscape-exporter-v1` | Export implementation identity |
| `serialization_version` | string | `audit-export-v2` | Canonical record serialization identity |
| `chunking_algorithm_version` | string | `record-framing-v1` | Chunk-boundary algorithm identity |
| `include_raw_error_rows` | bool | `false` | Include bounded raw error rows when policy permits |
| `total_record_limit` | int | required when enabled | Maximum records derived for one snapshot |
| `total_byte_limit` | int | required when enabled | Maximum serialized bytes derived for one snapshot |
| `chunk_limit` | int | required when enabled | Maximum immutable chunks in one snapshot |
| `per_chunk_record_limit` | int | required when enabled | Maximum records in one chunk |
| `per_chunk_byte_limit` | int | required when enabled | Maximum serialized bytes in one chunk |
| `spool_root` | path | required when enabled | Private path below `.elspeth/audit-export-spool`; absolute and parent-traversal paths are refused |
| `content_store` | object | required when enabled | Explicit durable immutable-content policy |
| `content_store.content_store_id` | string | required | Credential-free store identity persisted with snapshots |
| `content_store.namespace` | string | required | Credential-free content namespace |
| `content_store.root` | path | required | Private local content root below `.elspeth/audit-export-content-store` |
| `content_store.policy_version` | string | required | Retention/durability policy version |
| `content_store.retention_days` | int | required | Retention period |
| `content_store.durability` | string | required | `fsync` or `replicated` |

Enabled export is deliberately all-explicit: total capacity must fit within
`chunk_limit × per_chunk_*_limit`, and the spool must already be a private
directory. Content-store retention never authorizes deletion of referenced
snapshot objects.

**Signing and rotation:** `signer_key_id` is a public, credential-free identity
that includes the operator's key version. It participates in snapshot identity,
so rotating a key means selecting a new key ID and secret reference together.
`multi_version` preserves verification of old snapshots under their recorded
IDs; `single_export` refuses an identity change within that export lineage.
The value of `signing_secret_ref` is only the environment variable name. Key
bytes, secret values, hashes of weak key material, and low-entropy key-derived identifiers
are never persisted. See [Environment
Variables](environment-variables.md) for secret provisioning.

### Sink-effect resource and transport bounds

Effect adapters apply bounds before external publication. The relevant
settings and code-owned defaults are:

| Scope | Limit or setting | Current contract |
|---|---|---|
| Local file staging | bytes / rows | Code-owned maximum: 1 GiB and 10,000,000 rows per prepared effect |
| Local file target lock | lock wait | Code-owned bounded wait: 5 seconds; timeout aborts without publication |
| S3 object | `max_object_bytes`, `max_record_chars` | Defaults: 256 MiB and 1,000,000 characters; hard maxima: 1 GiB and 8,000,000 characters |
| Azure Blob object | `max_blob_bytes` | Default: 256 MiB; hard maximum: 1 GiB |
| Effect ownership | lease TTL | Coordinator default: 5 minutes; takeover is allowed only after expiry and increments the generation |
| Provider network | connect/read/request timeout | Must be bounded in the deployed provider client/SDK policy. These are not universal pipeline YAML fields; a transport failure becomes response-lost evidence and requires reconciliation, never assumed absence. |

Do not extend a network timeout beyond the operational lease window without a
coordinated lease/heartbeat policy. Do not raise staging or object limits by
bypassing validation: split the workload or change the reviewed adapter
contract.

### Remote effect body spool

S3 and Azure Blob effects stage their serialized bodies in a filesystem spool
before the conditional publish. The location is `ELSPETH_EFFECT_SPOOL_DIR`
when set, otherwise the project-local `.elspeth/sink-effect-spool/` (resolved
against the working directory, like the audit-export spool). Place the spool
on storage as durable as the landscape database — prepared plans reference
their staged bodies across process restarts. A body lost anyway (reboot,
tmp-cleaner) is re-derived from durable member payloads at commit time and
verified against the plan's sealed hash; a divergent re-derivation fails
closed. Do not repoint the spool while effects are in flight: plans seal their
stage path and a moved spool fails closed until the location is restored.

### JSONL Change Journal

Enable a redundant JSONL change journal for emergency backup. This is **not** the canonical audit record—use when you need a text-based, append-only backup stream.

```yaml
landscape:
  enabled: true
  url: sqlite:///./runs/audit.db

  # Enable the change journal
  dump_to_jsonl: true
  dump_to_jsonl_path: audit.journal.jsonl

  # Include LLM/HTTP request and response bodies
  dump_to_jsonl_include_payloads: true

  # Fail startup if committed journal backlog cannot be published (strict mode)
  dump_to_jsonl_fail_on_error: false
```

**Use cases:**

| Scenario | Recommended Settings |
|----------|---------------------|
| Debugging LLM calls | `dump_to_jsonl: true`, `include_payloads: true` |
| Compliance backup | `dump_to_jsonl: true`, `fail_on_error: true` |
| Production (minimal I/O) | `dump_to_jsonl: false` (default) |

**Notes:**
- Complete published records are append-only. Recovery may truncate only a
  recognized torn batch at EOF before replaying that batch from the outbox;
  unrelated or mid-file corruption fails closed
- Each line is a self-contained JSON record with durable `journal_batch_id`,
  `journal_batch_ordinal`, and `journal_batch_size` fields
- A transaction first stores its batch in `sidecar_journal_outbox`; only a
  successful database commit makes it publishable
- Publication fsyncs the sidecar before acknowledging the outbox row; startup
  drains committed backlog and batch IDs prevent duplicate publication after
  an append/ack crash window
- Failed database commits publish no records. A live sidecar I/O failure does
  not reverse an already successful database commit: the committed outbox row
  remains recoverable. With `fail_on_error: true`, an unrecoverable backlog
  fails the next database open instead of being silently skipped
- With `include_payloads: true`, LLM prompts and responses are embedded

### PostgreSQL Example

```yaml
landscape:
  backend: postgresql
  url: postgresql://user@host:5432/elspeth
```

**Note:** Passwords in URLs are fingerprinted (not stored) when `ELSPETH_FINGERPRINT_KEY` is set.

---

## Concurrency Settings

Configure parallel processing.

```yaml
concurrency:
  max_workers: 16
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_workers` | int | `4` | Maximum parallel workers |

**Recommendation:** Development: 4, Production: 16

---

## Rate Limit Settings

Limit external API calls to avoid throttling. Rate limits are applied at the **service level** - all plugins using the same service share the rate limit bucket.

```yaml
rate_limit:
  enabled: true
  default_requests_per_minute: 60
  persistence_path: ./rate_limits.db
  services:
    azure_openai:
      requests_per_minute: 100
    azure_content_safety:
      requests_per_minute: 50
    azure_prompt_shield:
      requests_per_minute: 50
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable rate limiting |
| `default_requests_per_minute` | int | `60` | Default per-minute limit for unconfigured services |
| `persistence_path` | string | - | SQLite path for cross-process rate limit state |
| `services` | object | `{}` | Per-service rate limit configurations |

### Service Rate Limit

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `requests_per_minute` | int | **Yes** | Maximum requests per minute for this service |

### Built-in Service Names

ELSPETH's built-in plugins use these service names for rate limiting:

| Service Name | Used By | Description |
|--------------|---------|-------------|
| `azure_openai` | `llm` (provider: azure) | Azure OpenAI API calls |
| `azure_content_safety` | `azure_content_safety` | Azure Content Safety API |
| `azure_prompt_shield` | `azure_prompt_shield` | Azure Prompt Shield API |

**Important:** Service names must use **underscores**, not hyphens (e.g., `azure_openai`, not `azure-openai`). This follows the internal validation pattern `^[a-zA-Z][a-zA-Z0-9_]*`.

### How Rate Limiting Works

1. **Configuration**: Define service limits in your pipeline YAML
2. **Registry**: The `RateLimitRegistry` creates limiters for each configured service
3. **Acquisition**: Plugins acquire rate limit tokens before making external calls
4. **Blocking**: When the limit is reached, calls block until capacity is available

Rate limits apply per-service across all uses in a pipeline. For example, if you have two `llm` transforms (provider: azure), they share the `azure_openai` rate limit.

### Example: Azure LLM Pipeline with Rate Limits

```yaml
sources:
  prompts:
    plugin: csv
    on_success: classify_in
    options:
      path: data/prompts.csv
      schema:
        mode: observed

transforms:
  # First LLM transform
  - name: classifier
    plugin: llm
    input: classify_in
    on_success: summarize_in
    on_error: discard
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      template: "Classify: {{ row.text }}"
      schema:
        mode: observed

  # Second LLM transform - shares rate limit with first
  - name: summarizer
    plugin: llm
    input: summarize_in
    on_success: output
    on_error: discard
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      template: "Summarize: {{ row.text }}"
      schema:
        mode: observed

sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/results.csv
      schema:
        mode: observed

# Rate limiting - both transforms share this limit
rate_limit:
  enabled: true
  services:
    azure_openai:
      requests_per_minute: 100  # 100 RPM shared across all llm (provider: azure) transforms
```

### Persistence for Distributed Systems

For multi-process or distributed deployments, configure `persistence_path` to share rate limit state:

```yaml
rate_limit:
  enabled: true
  persistence_path: /shared/rate_limits.db  # SQLite file on shared storage
  services:
    azure_openai:
      requests_per_minute: 100
```

This ensures rate limits are respected across multiple pipeline processes hitting the same external APIs.

### Two-Layer Rate Control (LLM Transforms)

LLM transforms like `llm` (provider: azure, with multiple queries) have **two complementary throttling mechanisms** working at different layers:

| Layer | Mechanism | Purpose | When It Acts |
|-------|-----------|---------|--------------|
| **Client** | `RateLimiter` | Proactive prevention | **Before** each API call |
| **Transform** | `PooledExecutor` with AIMD | Reactive handling | **After** receiving 429 |

These are **defense-in-depth**, not competing systems.

**Request Flow:**

```
                          Row arrives
                               │
                               ▼
              ┌────────────────────────────────┐
              │      BatchTransformMixin       │
              │   (row-level pipelining)       │
              └────────────────────────────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │      PooledExecutor            │
              │  (query-level parallelism)     │
              │  - Runs N queries in parallel  │
              │  - AIMD retry on 429           │
              └────────────────────────────────┘
                               │
                               ▼  (for each query)
              ┌────────────────────────────────┐
              │      AuditedLLMClient          │
              │  1. _acquire_rate_limit() ◄────┼── Blocks until RPM available
              │  2. Make API call              │
              │  3. Record to audit trail      │
              └────────────────────────────────┘
                               │
                               ▼
                          Azure API
```

**How each layer works:**

1. **RateLimiter (Proactive)** - Configured in YAML under `rate_limit.services`:
   - Blocks each API call until the configured RPM limit has capacity
   - Uses a sliding window (per minute)
   - Shared across all uses of the same service (e.g., `azure_openai`)
   - **Prevents** 429s through smooth, predictable throttling

2. **PooledExecutor AIMD (Reactive)** - Configured in plugin options:
   - Handles 429 errors that slip through (bursts, quota changes, shared quotas)
   - Uses AIMD backoff: multiply delay on 429, subtract on success
   - Retries until `max_capacity_retry_seconds` timeout
   - **Recovers** from 429s gracefully

**Configuration example with both layers:**

```yaml
# Proactive rate limiting (YAML config)
rate_limit:
  enabled: true
  services:
    azure_openai:
      requests_per_minute: 100  # Proactive: block before exceeding this rate

# Reactive handling (plugin options)
transforms:
  - name: multi_query_llm
    plugin: llm
    input: source_out
    on_success: output
    on_error: discard
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      queries:
        - template: "Classify: {{ row.text }}"
        - template: "Summarize: {{ row.text }}"
      pool_size: 8                      # Concurrent queries per row
      max_dispatch_delay_ms: 5000       # Max AIMD backoff delay (ms)
      max_capacity_retry_seconds: 3600  # Give up after 1 hour of 429s
      # ... other options
```

**Tuning guide:**

| Symptom | Tune This | How |
|---------|-----------|-----|
| Getting 429s frequently | `rate_limit.services.<name>.requests_per_minute` | Lower the RPM |
| Queries too slow (blocking unnecessarily) | `rate_limit.services.<name>.requests_per_minute` | Raise RPM (if quota allows) |
| 429 recovery too aggressive | Plugin `max_dispatch_delay_ms` | Increase max backoff |
| 429s causing row failures | Plugin `max_capacity_retry_seconds` | Increase retry timeout |

**Key insight:** RateLimiter is your first line of defense (smooth, predictable throttling), while PooledExecutor AIMD is your safety net (handles bursts and quota changes gracefully).

---

## Telemetry Settings

Configure operational telemetry exports (OTLP, Azure Monitor, Datadog, console). Telemetry provides **real-time operational visibility** alongside the Landscape audit trail.

**Key distinction:**
- **Landscape**: Legal record, complete lineage, persisted forever, source of truth
- **Telemetry**: Operational visibility, real-time streaming, ephemeral, for dashboards/alerting

```yaml
telemetry:
  enabled: true
  granularity: rows
  backpressure_mode: block
  fail_on_total_exporter_failure: false
  exporters:
    - name: otlp
      options:
        endpoint: http://localhost:4317
        headers:
          Authorization: "Bearer ${OTEL_TOKEN}"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable telemetry emission |
| `granularity` | string | `lifecycle` | Event verbosity: `lifecycle`, `rows`, or `full` |
| `backpressure_mode` | string | `block` | How to handle slow exporters: `block`, `drop`, or `slow` |
| `fail_on_total_exporter_failure` | bool | `true` | Crash run if all exporters fail repeatedly |
| `exporters` | list | `[]` | Exporter configurations |

### Granularity Levels

| Level | Events Emitted | Volume | Use Case |
|-------|----------------|--------|----------|
| `lifecycle` | Run start/complete, phase transitions | ~10-20/run | Production monitoring |
| `rows` | Lifecycle + row creation, transform completion, gate routing, field-resolution mapping | N × M events | Debugging, progress tracking |
| `full` | Rows + external call details (LLM prompts/responses, HTTP, SQL) | High | Deep debugging, call analysis |

**Choosing a granularity:**

```yaml
# Production: minimal overhead, just run lifecycle
telemetry:
  enabled: true
  granularity: lifecycle
  exporters:
    - name: datadog

# Development: see row-by-row progress
telemetry:
  enabled: true
  granularity: rows
  exporters:
    - name: console
      options:
        format: pretty

# Debugging LLM issues: full call details
telemetry:
  enabled: true
  granularity: full
  exporters:
    - name: console
      options:
        format: json
```

### Backpressure Modes

| Mode | Behavior | Trade-off |
|------|----------|-----------|
| `block` | Block pipeline when exporters can't keep up | Complete telemetry, may slow pipeline |
| `drop` | Drop events when buffer is full | No pipeline impact, lossy telemetry |
| `slow` | Adaptive rate limiting | (Not yet implemented) |

**Recommendation:** Use `block` for debugging sessions (complete data), `drop` for production (no pipeline impact).

### Exporter Configuration

Each exporter config has a required name and an `options` block. Exporter-specific keys **must** be placed under `options`.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | **Yes** | `console`, `otlp`, `azure_monitor`, `datadog` |
| `options` | object | No | Exporter-specific settings (see below) |

**Secrets convention:** keep non-sensitive values in YAML and secrets in `.env`, referenced with `${VAR}`. For example, `options.endpoint` can be set in YAML while `options.headers.Authorization` comes from `.env`.

### Built-in Exporter Options

**Console**
```yaml
options:
  format: json   # json | pretty
  output: stdout # stdout | stderr
```

**OTLP**
```yaml
options:
  endpoint: http://localhost:4317   # required
  headers:
    Authorization: "Bearer ${OTEL_TOKEN}"  # optional
  batch_size: 100                   # optional
```

**Azure Monitor**
```yaml
options:
  connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}  # required
  batch_size: 100                                              # optional
```

**Datadog**
```yaml
options:
  service_name: "elspeth"         # optional
  env: "production"               # optional
  agent_host: "localhost"         # optional
  agent_port: 8126                # optional
  version: "1.0.0"                # optional
```

### Correlation with Audit Trail

Telemetry events include `run_id` and `token_id` fields that correlate directly with the Landscape audit database. This enables tracing from operational alerts to full lineage investigation.

**Workflow:**

1. **Alert fires** in Datadog/Grafana (e.g., "high error rate on transform X")
2. **Extract `run_id`** from the telemetry event
3. **Investigate with `explain`** command:
   ```bash
   elspeth explain --run <run_id> --database ./runs/audit.db
   ```
4. **Or use the Landscape MCP server** for programmatic access:
   ```bash
   elspeth-mcp --database ./runs/audit.db
   # Then: get_failure_context(run_id)
   ```

**Key correlation fields in telemetry events:**

| Field | Description | Maps To |
|-------|-------------|---------|
| `run_id` | Pipeline execution identifier | `runs.run_id` |
| `token_id` | Row instance identifier | `node_states.token_id` |
| `node_id` | Transform/gate instance | `nodes.node_id` |
| `state_id` | Processing state record | `node_states.state_id` |

---

## Checkpoint Settings

Configure crash recovery checkpointing.

```yaml
checkpoint:
  enabled: true
  frequency: every_n
  checkpoint_interval: 100
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable checkpointing |
| `frequency` | string | `every_row` | Checkpoint frequency |
| `checkpoint_interval` | int | - | Row interval (required for `every_n`) |

The defaults above are the programmatic fallback for omitted settings. Checked-in
pipeline configs that make durability or performance claims should declare
`checkpoint` explicitly and should record the selected frequency with benchmark
results.

### Frequency Options

| Frequency | Behavior | Trade-off |
|-----------|----------|-----------|
| `every_row` | Checkpoint after each row | Safest, higher I/O |
| `every_n` | Checkpoint every N rows | Balance safety/performance |
| `aggregation_only` | Checkpoint at aggregation flushes only | Fastest, lose up to batch on crash |

---

## Retry Settings

Configure retry behavior for transient failures.

```yaml
retry:
  max_attempts: 3
  initial_delay_seconds: 1.0
  max_delay_seconds: 60.0
  exponential_base: 2.0
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_attempts` | int | `3` | Maximum retry attempts |
| `initial_delay_seconds` | float | `1.0` | Initial backoff delay |
| `max_delay_seconds` | float | `60.0` | Maximum backoff delay |
| `exponential_base` | float | `2.0` | Exponential backoff base |

Delay calculation: `min(initial_delay * base^attempt, max_delay)`

---

## Payload Store Settings

Configure storage for large binary payloads.

```yaml
payload_store:
  backend: filesystem
  base_path: .elspeth/payloads
  retention_days: 90
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | string | `filesystem` | Storage backend |
| `base_path` | path | `.elspeth/payloads` | Base path for filesystem backend |
| `retention_days` | int | `90` | Payload retention in days |

---

## Environment Variables

See the [Environment Variables Reference](environment-variables.md) for the complete list of supported environment variables, including:

- **Security variables:** `ELSPETH_FINGERPRINT_KEY`, `ELSPETH_SIGNING_KEY`
- **LLM provider keys:** `OPENROUTER_API_KEY`, `AZURE_OPENAI_API_KEY`
- **Azure service credentials:** Content Safety, Prompt Shield, Blob Storage
- **Telemetry credentials:** `OTEL_TOKEN`, `APPLICATIONINSIGHTS_CONNECTION_STRING`, `DD_API_KEY`
- **Secret field detection patterns**

Configuration is loaded with this precedence (highest first):
1. Environment variables (`ELSPETH_*`)
2. Config file (settings.yaml)
3. Pydantic schema defaults

Nested environment variables use double underscore: `ELSPETH_LANDSCAPE__URL`.

---

## Expression Syntax

Gate conditions, aggregation triggers, commencement-gate conditions, and
`value_transform` operations use a restricted expression language.

Row-scoped expressions (gates, aggregation triggers, `value_transform`
operations) bind a single name, `row`. Commencement-gate conditions run before
any row exists and bind a different set of names —
[Commencement Gates](#commencement-gates) documents them — but they are parsed
by the same grammar, so everything below applies to them too.

### Allowed Constructs

| Construct | Example |
|-----------|---------|
| Field access | `row['field']`, `row.get('field')` |
| Built-in functions | `len()`, `abs()` |
| Comparisons | `==`, `!=`, `<`, `<=`, `>`, `>=` |
| Boolean operators | `and`, `or`, `not` |
| Arithmetic | `+`, `-`, `*`, `/`, `%` |
| Membership | `in`, `not in` |
| Literals | `True`, `False`, `None`, numbers, strings |
| Ternary | `x if condition else y` |

**`row.get()` does not accept default values.** `row.get('field')` returns `None` if the key is missing. `row.get('field', 'fallback')` is **forbidden** — default values fabricate data the source never provided. Use `row.get('field') is not None` to test for field presence.

### Forbidden Constructs

| Forbidden | Reason |
|-----------|--------|
| Coercive function calls (`int()`, `str()`, `float()`, `bool()`) | Not needed — the source schema guarantees type safety before expressions run |
| Imports | Security |
| Lambda expressions | Security |
| Comprehensions | Security |
| Attribute access (except `row.get()`) | Security |
| F-strings | Security |

### Type Safety

Type coercion functions like `int()` are not needed in expressions. The source schema handles type conversion at the boundary — by the time data reaches a gate or trigger, fields already have the types declared in the schema:

```yaml
sources:
  transactions:
    plugin: csv
    on_success: raw_data
    options:
      schema:
        fields:
          - "amount: int"  # CSV strings are coerced to int at load time

gates:
  - name: threshold
    condition: "row['amount'] > 1000"  # amount is guaranteed to be int here
```

---

## Complete Example

```yaml
# Sources - where data comes from (one or more named sources)
sources:
  transactions:
    plugin: csv
    on_success: raw_data
    options:
      path: data/transactions.csv
      schema:
        mode: fixed
        fields:
          - "id: int"
          - "amount: int"
          - "customer_id: str"
      on_validation_failure: quarantine

# Sinks - where data goes
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/normal.csv
      schema:
        mode: observed

  high_values:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/high_values.csv
      schema:
        mode: observed

  quarantine:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/quarantine.csv
      schema:
        mode: observed

# Transforms
transforms:
  - name: enricher
    plugin: field_mapper
    input: raw_data
    on_success: flagging_in
    on_error: quarantine
    options:
      schema:
        mode: observed
      mapping:
        customer_id: account_id

  - name: flag_large_orders
    plugin: value_transform
    input: flagging_in
    on_success: enriched
    on_error: quarantine
    options:
      schema:
        mode: observed
      operations:
        - target: is_large_order
          expression: "row['amount'] >= 1000"

# Gates - routing decisions
gates:
  - name: amount_threshold
    input: enriched
    condition: "row['amount'] > 1000"
    routes:
      "true": high_values
      "false": output

# Audit trail
landscape:
  enabled: true
  backend: sqlite
  url: sqlite:///./runs/audit.db
  export:
    enabled: false

# Operational settings
concurrency:
  max_workers: 4

checkpoint:
  enabled: true
  frequency: every_row

retry:
  max_attempts: 3
  initial_delay_seconds: 1.0
  max_delay_seconds: 60.0
  exponential_base: 2.0

rate_limit:
  enabled: true
  default_requests_per_minute: 60
  services:
    azure_openai:
      requests_per_minute: 100

payload_store:
  backend: filesystem
  base_path: .elspeth/payloads
  retention_days: 90

telemetry:
  enabled: true
  granularity: rows
  exporters:
    - name: console
      options:
        format: pretty
    - name: otlp
      options:
        endpoint: http://localhost:4317
        headers:
          Authorization: "Bearer ${OTEL_TOKEN}"
```

---

## See Also

- [Your First Pipeline](../guides/your-first-pipeline.md) - Getting started tutorial
- [Docker Guide](../guides/docker.md) - Container deployment
- [PLUGIN.md](../../PLUGIN.md) - Plugin development
