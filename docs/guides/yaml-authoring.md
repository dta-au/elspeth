# Authoring pipelines in YAML

Use a settings file when you want to write, review, and version a pipeline as
text. YAML and the Web Composer feed the same plugin contracts, graph validator,
runtime, and Landscape audit trail. You do not need Composer or an LLM provider
to run a pipeline made entirely from local data plugins.

This guide describes the 0.8.6 YAML interface. It assumes ELSPETH is already
installed in an activated environment; check with `elspeth --version`. For
source installation, see [Your First Pipeline](your-first-pipeline.md#step-1-install-elspeth).
Use an approved checkout or installed version that supports the features you
select.

## Build a small local pipeline

This walkthrough reads three transactions, computes an amount including a fee,
and routes each row to a normal or high-value CSV file. It makes no network calls.

```text
input.csv ── fee_in ── value_transform ── threshold_in ── gate
                                                        ├─ normal.csv
                                                        └─ high.csv
                         Landscape records the run and row decisions
```

Create a fresh working directory so previous outputs cannot affect the result.
Keep this shell open for the following commands:

```bash
DEMO_DIR="$(mktemp -d)"
cd "$DEMO_DIR"
mkdir output runs
cat > input.csv <<'CSV'
id,amount
1,500
2,1500
3,995
CSV
```

Save this complete configuration as `settings.yaml` in that directory:

```yaml
sources:
  transactions:
    plugin: csv
    on_success: fee_in
    options:
      path: input.csv
      on_validation_failure: discard
      schema:
        mode: fixed
        fields: ["id: int", "amount: int"]

transforms:
  - name: add_fee
    plugin: value_transform
    input: fee_in
    on_success: threshold_in
    on_error: discard
    options:
      schema: {mode: observed}
      operations:
        - target: amount_with_fee
          expression: "row['amount'] + 10"

gates:
  - name: amount_threshold
    input: threshold_in
    condition: "row['amount_with_fee'] > 1000"
    routes:
      "true": high
      "false": normal
    on_error: discard

sinks:
  normal:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/normal.csv
      collision_policy: fail_if_exists
      schema: {mode: observed}
  high:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/high.csv
      collision_policy: fail_if_exists
      schema: {mode: observed}

landscape:
  url: sqlite:///runs/audit.db

payload_store:
  base_path: runs/payloads

concurrency:
  max_workers: 1
```

Validate, preview, then execute from the same working directory:

```bash
cd "$DEMO_DIR"
elspeth validate --settings settings.yaml
elspeth run --settings settings.yaml --dry-run
elspeth run --settings settings.yaml --execute
cat output/normal.csv output/high.csv
```

The normal file contains ID 1 with `amount_with_fee` 510. The high file contains
IDs 2 and 3 with 1510 and 1005. The run reports three processed rows. Check both
the exit status and the actual outputs: a pipeline can complete with fewer
published rows because a gate intentionally discarded some of them.

`--execute` starts processing; `--dry-run` validates and describes the pipeline
without running its rows. Calling `run` with neither flag does not execute and
exits nonzero with instructions to add `--execute`. Use console output for a
dry run; `run --dry-run --format json` currently exits nonzero.

For another small complete example, see
[threshold routing](../../examples/threshold_gate/settings.yaml).

## Understand the settings file

| Section | What you author |
| --- | --- |
| `sources` | A mapping of source names to input plugins, options, and success destinations. At least one source is required. |
| `transforms` | Named plugin nodes, each with `input`, `on_success`, `on_error`, and plugin-specific `options`. |
| `gates` | Engine expressions that choose destinations or fork a row. |
| `queues`, `coalesce`, `row_unions`, `collectors`, `scopes`, `aggregations` | Explicit fan-in, fork joins, expansion groups, and batching, described below. |
| `sinks` | A mapping of destination names to output plugins, options, and required `on_write_failure` decisions. |
| `landscape` | The audit database connection. |
| `payload_store`, `checkpoint` | Retained row/blob payloads and recovery settings. Keep them with the audit store if you need resume. |
| `concurrency`, `retry`, `rate_limit` | Scheduling, transient retry, and service rate controls. |
| `depends_on`, `collection_probes`, `commencement_gates` | Other pipelines and readiness checks that run before row processing. |

See the [Configuration Reference](../reference/configuration.md) for field
defaults and operational sections. Unknown top-level or node-envelope fields
are rejected. Routing fields belong on the node; `schema`, file paths, prompts,
and plugin knobs belong under `options`. In particular, source validation policy
is `sources.<name>.options.on_validation_failure`, while transform error policy
is `transforms[].on_error` and sink write policy is `sinks.<name>.on_write_failure`.

The YAML examples below are section fragments. Merge their entries into your
existing lists or mappings, and connect each stated input to an upstream
destination. Do not append duplicate top-level YAML keys such as `transforms`
or `sinks`. The walkthrough above and linked settings files are complete examples.

### Wire nodes with named connections

A node's `name` identifies it in the graph and audit. Its `input` identifies
the connection it consumes. An upstream `on_success` or gate route produces
that connection. The spelling must match exactly. A destination may instead
be a sink key, which writes the row immediately.

In the walkthrough, the source produces `fee_in`; `add_fee` consumes it and
produces `threshold_in`; `amount_threshold` consumes that connection and sends
rows to the `normal` or `high` sink. Moving the transform above or below the
gate in the YAML does not change those connections. There is no implicit
"next item" route and the historical destination `continue` is unsupported.

Use simple distinct identifiers for node names, connections, branches, and sink
keys. Do not use system-prefixed names beginning `__`. Every successful path
needs an explicit destination, and every declared graph node must be reachable
from a source. Remove unused sinks rather than leaving placeholders. If
multiple producers feed one processing
connection, declare a queue rather than relying on list order or silently
merging their streams. Forking also requires an explicit fork gate.

### Choose schemas deliberately

| `schema.mode` | Use it when |
| --- | --- |
| `fixed` | You know the exact fields; undeclared extra fields should fail validation. |
| `flexible` | You require declared fields but allow additional ones. |
| `observed` | You want a contract inferred from data rather than an explicit field list. |

Explicit fields use strings such as `"id: int"`, `"text: str"`, or
`"amount: float"`. CSV source validation performs the declared conversion;
downstream transforms should use `type_coerce` for additional normalization.
An observed schema does not mean every later row shape is accepted: the first
valid row establishes a contract. Observed and flexible inference is bounded
to 1024 total fields; declare a full fixed schema or narrow a wider input.

For observed contracts, a producer can declare `schema.guaranteed_fields`, and
a consumer can declare `schema.required_fields`. Plugins also expose their
own requirements, such as LLM `required_input_fields`. Build-time admission
checks that upstream guarantees satisfy downstream requirements. Explicit
fixed/flexible fields provide guarantees automatically.

Treat `guaranteed_fields` as the complete stable field API, not a partial hint.
For example, guaranteeing only `id` does not prove that a sink can require
`colour`, even if the data also contains it. With no observed guarantees,
some sink requirements are deferred to per-row validation; consumers with
explicit build-time input requirements still need a sufficient producer contract.

CSV headers are normalized (`First Name` becomes `first_name`, `Amount`
becomes `amount`). Use those carried names in schema declarations, field
options, and `required_input_fields`. See
[field-name declarations](../reference/configuration.md#field-names-in-declarations).

A transform's `options.schema` describes its input. Its plugin declares what
it produces. When adding fields, make sure the downstream sink accepts them:
the walkthrough's observed sinks accept `amount_with_fee`; a fixed sink listing
only `id` and `amount` would reject that extra field. A `field_mapper` with
`select_only: true` is useful when you want a smaller final record.

### Keep paths predictable

Local source/sink paths, relative SQLite URLs, and payload paths resolve from
the process working directory. Passing `--settings config/settings.yaml` does
not change that directory. Run repository examples from the repository root,
because their input/output paths begin with `examples/`.

File-backed options `template_file`, `system_prompt_file`, `lookup_file`, and
`reference_file` instead resolve relative to the settings file and must remain
inside its directory tree. Dependency `settings` paths are also relative to
the parent settings file and confined to the main configuration directory tree.
Use container-visible paths when running inside a container.

Choose a file sink's collision behavior explicitly: `fail_if_exists` protects
an existing target; `auto_increment` selects a free sibling path. For deliberate
append use the plugin's `mode: append` with `collision_policy: append_or_create`.
Do not rerun the walkthrough over its existing files with `fail_if_exists`;
create a fresh demo directory or choose new output paths.

## Choose plugins by job

The installed registry is the complete plugin inventory. Optional dependencies
can affect availability. Query it before copying a plugin name:

```bash
elspeth plugins list
elspeth plugins list --type transform
elspeth plugins list --format json
elspeth plugins inspect source csv
elspeth plugins inspect transform value_transform --format json
elspeth plugins inspect sink json
```

`inspect` gives the selected plugin's description, configuration fields, and
JSON Schema. The [Configuration Reference](../reference/configuration.md) explains
cross-plugin settings and provider-specific contracts. Start with these families;
the example links contain complete settings files and prerequisite notes.

| Job | Plugin choices | Configuration starting point |
| --- | --- | --- |
| Local row input | Sources `csv`, `json`, `text`; `null` emits no rows | `path`, `schema`, `on_validation_failure`; JSONL uses `json` with `format: jsonl`. [Threshold example](../../examples/threshold_gate/README.md). |
| Managed file input | Source `blob_rows`; transforms `blob_fetch`, `blob_csv_expand`, `blob_json_expand`, `blob_text_expand` | Managed blob references, payload store, and parser options. [Blob examples](../../examples/blob_transforms/README.md). |
| Cloud/business input | Sources `aws_s3`, `azure_blob`, `dataverse`, `power_automate` | Object/table/flow identity, credentials, schema, and bounded transfer controls. [S3 contract](../reference/configuration.md#available-source-plugins), [Power Automate](../../examples/power_automate/README.md). |
| Rename, calculate, normalize | Transforms `field_mapper`, `value_transform`, `type_coerce`, `truncate`, `passthrough` | `mapping`/`select_only`, `operations`, `conversions`, or `fields`. [Reference enrichment and calculations](../../examples/reference_join/README.md). |
| Filter or expand rows | Transforms `keyword_filter`, `json_explode`, `line_explode`; batch plugin `batch_replicate` | Content patterns, array/text fields, or copies field. Use a gate for expression routing; use a scope/collector for per-parent expansion results. [JSON explode](../../examples/json_explode/README.md), [deaggregation](../../examples/deaggregation/README.md). |
| Lookup or fetch context | Transforms `reference_join`, `web_scrape`, `rag_retrieval`, `azure_ai_search` | Reference table and match key, URL/query field, or retrieval provider/index. [Reference joins](../../examples/reference_join/README.md), [web scraping](../reference/web-scrape-transform.md), [Chroma](../../examples/chroma_rag/README.md). Network/retrieval plugins have extra prerequisites. |
| LLM generation | Source `llm` for one authored prompt; transform `llm` for a prompt per arriving row | Provider/profile, prompt, input declarations, response shape, capacity controls. [LLM source](../../examples/llm_source/README.md) and [queries below](#author-llm-queries). |
| Document extraction | `pdf_rasterize`, `azure_document_intelligence`, `aws_textract_document_analysis`, `aws_textract_inline_analysis` | Managed blobs or service document references, page/extraction options. [PDF rasterization](../../examples/pdf_rasterize/README.md), [Textract](../../examples/textract_inline/README.md). Service extraction is billable. |
| Content controls | `azure_content_safety`, `azure_prompt_shield`, `aws_bedrock_content_safety`, `aws_bedrock_prompt_shield` | Declared text fields, service credentials, thresholds or guardrail identity. [Required controls](../reference/configuration.md#required-controls-and-error-routing). |
| Batch statistics/comparison | `batch_stats`, `batch_rank`, `batch_classifier_metrics`, `batch_data_quality_report`, `batch_distribution_profile`, `batch_drift_compare`, `batch_effect_size`, `batch_experiment_compare`, `batch_outlier_annotator`, `batch_paired_preference`, `batch_threshold_summary`, `batch_top_k` | Put batch-aware plugins in `aggregations` or a bound `collector`, then select value/group/variant fields. [Statistics examples](../../examples/statistical_batch_plugins/README.md). |
| Reports and local output | Batch transform `report_assemble`; sinks `csv`, `json`, `text`, `document` | File path, schema, collision policy; text/document select one string field. `text` writes LF-delimited records; `document` writes one whole field value. [Report assembly](../../examples/report_assemble/README.md). |
| Database, cloud, or integration output | Sinks `database`, `aws_s3`, `azure_blob`, `chroma_sink`, `dataverse`, `power_automate` | Table/object/collection/flow identity and credentials. Check the plugin's execution and resume contracts. [Database sink](../../examples/database_sink/README.md), [Power Automate](../../examples/power_automate/README.md). |

For a small mapping node, this fragment consumes `selected_in` and sends a
record containing only `transaction_id` and `amount` to an existing `output` sink:

```yaml
transforms:
  - name: select_output
    plugin: field_mapper
    input: selected_in
    on_success: output
    on_error: discard
    options:
      schema: {mode: observed}
      mapping: {id: transaction_id, amount: amount}
      select_only: true
```

The mapping direction is arriving field → output field. For calculations use
`value_transform.operations`, as in the walkthrough; `field_mapper` does not
evaluate expressions. For a static reference table use `reference_join`, not
a second source plus an assumed SQL join. Multiple independent sources can
feed a queue, but a queue does not join records by key.

## Route, fork, join, and batch

These are the supported YAML control constructs. Gates, queues, coalesce, and
row unions are engine operations; aggregations and collectors use registered
batch-aware transform plugins. There is no generic `branch:` or `batch:` node
type to put under `transforms`.

### Gates: choose one destination

The walkthrough shows a boolean gate. Quote `"true"` and `"false"` so YAML
keeps them as string route labels. A condition may instead return a string
matching a route label, for example:

```yaml
gates:
  - name: classify_amount
    input: classified_in
    condition: "'high' if row['amount'] > 1000 else 'normal'"
    routes:
      high: high
      normal: normal
    on_error: discard
```

Expressions use a restricted grammar, not arbitrary Python: comparisons,
arithmetic, boolean operators, conditional expressions, subscripts, and approved
builtins. Use `row['amount']` rather than arbitrary attribute access or imports.
See [Expression Syntax](../reference/configuration.md#expression-syntax).
Every possible result needs a route. A route to `discard` is an intentional,
audited successful decision to end that branch. A gate's `on_error: discard`
records a failed expression evaluation. Omitting gate `on_error` preserves
fail-fast behavior for row-scoped evaluation failures.

### Forks and coalesce: one row, several analyses, one merged result

Forking is a gate operation. `fork_to` names branch identities and their initial
connections. Each branch may run its own transform chain. A coalesce maps each
original branch identity to the connection where that branch now arrives.

This fragment forks every row, passes both branches through unchanged, and
emits one nested result to an existing `output` sink:

```yaml
gates:
  - name: parallel_analysis
    input: fork_in
    condition: "True"
    routes: {"true": fork, "false": output}
    fork_to: [left, right]

transforms:
  - name: analyze_left
    plugin: passthrough
    input: left
    on_success: left_done
    on_error: discard
    options:
      schema: {mode: observed}
  - name: analyze_right
    plugin: passthrough
    input: right
    on_success: right_done
    on_error: discard
    options:
      schema: {mode: observed}

coalesce:
  - name: merge_analysis
    branches: {left: left_done, right: right_done}
    policy: require_all
    merge: nested
    on_success: output
```

The result has `left` and `right` objects. Coalesce correlates siblings from
the same forked source row, not unrelated rows with equal business keys.

| Coalesce choice | Meaning |
| --- | --- |
| `policy: require_all` | Require every branch. |
| `policy: quorum` + `quorum_count` | Require the specified number of branches. |
| `policy: best_effort` + `timeout_seconds` | Permit a partial result under the configured wait. |
| `policy: first` | Use the first arrival. |
| `merge: nested` | Preserve each branch's row under its branch name. |
| `merge: union` | Combine fields; choose `union_collision_policy: last_wins`, `first_wins`, or `fail`. The default is `last_wins` in declared branch order. |
| `merge: select` + `select_branch` | Use the named branch's row. |

Branch arrival policy and union field-collision policy are separate decisions.
If both branches carry `id` or `amount`, `union_collision_policy: fail` rejects
that overlap even when the values agree. Use `nested` when both full rows are
useful. See [branch transforms](../../examples/fork_coalesce/settings_per_branch.yaml)
and [coalesce settings](../reference/configuration.md#coalesce-settings).

### Row unions: preserve all fork observations

Use `row_unions` when you want one row per branch, such as control and treatment
observations for an experiment. Replace the coalesce in the fragment above with:

```yaml
row_unions:
  - name: paired_results
    branches: {left: left_done, right: right_done}
    on_success: comparison_in
```

This releases both original branch rows together, in declared branch order,
without merging fields or deduplicating. All branches must arrive; a lost,
duplicate, or late branch fails the whole group. Optional `timeout_seconds`
bounds the wait. Its destination must be a processing connection, not a sink.

An aggregation downstream of a row union must use end-of-source-only flushing
(`trigger: {}` or omit `trigger`), because an early trigger could split a pair.
The [offline A/B example](../../examples/row_union_ab_experiment/settings.yaml)
connects a fork, branch calculations, row union, and `batch_experiment_compare`.

### Queues: interleave independent streams

Declare `queues: {merged: {}}`, point multiple source/node `on_success` values
at `merged`, and set the downstream node's `input: merged`.

```yaml
queues:
  merged:
    description: Transactions from both input streams
```

A queue is pass-through scheduling fan-in. It does not merge fields, join
records, or group fork siblings. Every producer must satisfy the downstream
input contract; do not depend on a particular interleaving order. The
[concurrent scheduler example](../../examples/concurrent_scheduler/settings.yaml)
shows two CSV sources feeding one queue and aggregation.

### Aggregations: process a window or whole stream

This fragment consumes amounts from `batch_in`, flushes every five accepted
rows, and sends statistics to an existing `output` sink:

```yaml
aggregations:
  - name: amount_stats
    plugin: batch_stats
    input: batch_in
    on_success: output
    on_error: discard
    trigger: {count: 5}
    output_mode: transform
    options:
      schema: {mode: observed}
      value_field: amount
      compute_mean: true
```

Triggers may use `count`, `timeout_seconds`, or a `condition` over batch metrics
`row['batch_count']` and `row['batch_age_seconds']`. Combine them for first-to-fire
behavior. Remaining rows flush at source exhaustion. To summarize the entire
stream, omit `trigger` or set it to `{}`; `condition: end_of_source` is invalid.
Trigger conditions cannot inspect an individual row's business fields.

`output_mode: transform` publishes the batch plugin's result rows; optionally
set `expected_output_count` to enforce an exact result count. `passthrough`
releases accepted input rows unchanged and does not accept that count check.
If a batch flush returns an error, every input row in that batch goes through
the aggregation's `on_error` policy with its original values.

An aggregation's `group_by` plugin option groups within each flushed batch;
it does not turn a count window into a complete business-object group. See
[batch aggregation](../../examples/batch_aggregation/settings.yaml).

### Scopes and collectors: finish one parent's expansion

Use a scope when one row expands into children and you need one result for that
parent. For example, expand a document's `pages`, process each page, and collect
the page readings. The scope binds the expansion node name to a collector name:

```yaml
transforms:
  - name: expand_pages
    plugin: json_explode
    input: documents_in
    on_success: page_values_in
    on_error: discard
    options:
      schema: {mode: observed}
      array_field: pages
      output_field: page
  - name: read_page
    plugin: value_transform
    input: page_values_in
    on_success: readings_in
    on_error: discard
    options:
      schema: {mode: observed}
      operations:
        - target: reading
          expression: "row['page']['reading'] + 0"

collectors:
  - name: summarize_document
    plugin: batch_stats
    input: readings_in
    on_success: output
    options:
      schema: {mode: observed}
      value_field: reading

scopes:
  - name: document_pages
    opener: expand_pages
    closer: summarize_document
    policy: require_all
```

A collector flushes at the end of that one expansion group in expansion order.
It has no `trigger` and accepts no `on_error`. A count-based aggregation cannot
substitute for it: missing children could otherwise be replaced by the next
parent's children. The scope policy is required: `require_all` refuses an
incomplete group; `best_effort` permits results from surviving members with
the loss still audited. Collector plugin failure is settled as a whole-group
verdict through scope policy and nesting.

Only a transform that creates child tokens can be a scope opener. Forks can
nest inside expansion groups, but bound regions must have structurally valid
closers. The default supported nesting depth is five. See the
[scope/collector example](../../examples/scope_collector/README.md) for complete
`require_all` and `best_effort` configurations and expected partial-run results.

### Before the run: dependencies and commencement gates

`depends_on` runs named pipeline settings files before the main pipeline.
`collection_probes` checks vector collections; `commencement_gates` evaluates
go/no-go expressions after dependencies/probes. These gates have no row and use
`collections`, `dependency_runs`, and explicitly supplied `env` context.

For example, the [indexed Chroma workflow](../../examples/chroma_rag_indexed/README.md)
runs indexing before queries and checks collection readiness. See
[pipeline dependencies](../reference/configuration.md#pipeline-dependencies)
for the configuration. Dependencies and probes are execution prerequisites;
config validation does not prove their live readiness.

## Choose what happens to failures

`quarantine` is a name you give a sink, not a magic plugin or destination.
Define it under `sinks` before routing errors there. For example, use a JSONL sink:

```yaml
sinks:
  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: output/quarantine.jsonl
      format: jsonl
      collision_policy: fail_if_exists
      schema: {mode: observed}
```

| Policy | When it applies | Choice |
| --- | --- | --- |
| Source `options.on_validation_failure` | Input cannot satisfy the source schema | Named quarantine sink, or `discard`. |
| Transform `on_error` | A row cannot be processed | Named sink, or `discard`; inside a bound region the appropriate closer can also be a valid structural destination. |
| Gate `on_error` | A valid expression fails on a particular row | Named sink, `discard`, or omit to stop the run. Invalid expression syntax always fails configuration validation. |
| Aggregation `on_error` | A batch flush fails | Named sink or `discard`, applied to every original batch input row. |
| Sink `on_write_failure` | A routable per-row write failure | `discard`, or a failsink backed by `csv` or `json`. Other sink plugins are not eligible failsinks. |

Routing a failed row to a file preserves the data for inspection; it does not
turn that row into a successful result. Failure reasons remain in Landscape,
so do not assume an error sink adds an `error` column to the original row.
`discard` retains audit evidence but writes no recovery copy to an error sink.
Sink contract violations and uncertain external effects can stop or refuse the
run; `on_write_failure` is not a blanket exception handler.

Configure transient retries separately:

```yaml
retry:
  max_attempts: 3
  initial_delay_seconds: 1.0
  max_delay_seconds: 60.0
  exponential_base: 2.0
```

Retries apply to retryable failures, not arbitrary bad rows or plugin bugs.
LLM plugins also have capacity retry controls such as
`max_capacity_retry_seconds`; choose them alongside model timeouts and service
rate limits. External calls can be repeated during retry or recovery. See
[Retry Settings](../reference/configuration.md#retry-settings) and
[Data Trust and Error Handling](data-trust-and-error-handling.md).

## Author LLM queries

An LLM source generates from one authored prompt. An LLM transform consumes
arriving rows. Both require a supported provider binding and the operator's
credentials and service access. They are not needed for the local walkthrough.

For a single-query transform, declare the upstream columns you read and use
`row` in the Jinja template. This fragment needs an upstream connection named
`text_in` guaranteeing `text`, and existing `output` and `quarantine` sinks:

```yaml
transforms:
  - name: summarize
    plugin: llm
    input: text_in
    on_success: output
    on_error: quarantine
    options:
      provider: openrouter
      model: openai/gpt-4.1-mini
      api_key: ${OPENROUTER_API_KEY}
      schema: {mode: observed}
      required_input_fields: [text]
      prompt_template: "Summarize this text: {{ row.text }}"
      response_field: summary
      max_tokens: 200
      max_capacity_retry_seconds: 30
```

The model is an example binding; confirm availability and options with your
provider. Azure uses its endpoint/deployment configuration, Bedrock uses its
region/model and supported AWS credential chain, and `gateway` targets the
separately deployed compatibility gateway. For an operator-maintained catalog,
CLI nodes can select an `llm_profiles` alias with `options.profile` instead of
authoring provider details. See the
[LLM configuration](../reference/configuration.md#llm-transform-llm).

### Several independent questions

Add `queries` when one node should ask several questions about the same row.
The default `query_input_mode: upstream` gives every query the original input,
even if the queries are written in sequence. A query's `input_fields` maps its
template variable names to arriving row columns. Each query emits prefixed
fields such as `classify_answer`, not a shared unprefixed `answer` field.

### A later question needs an earlier result

Use `query_input_mode: ordered`, a list of uniquely named queries, and
`pool_size: 1`. Replace the single-query prompt options above with these options
while retaining the provider binding:

```yaml
query_input_mode: ordered
pool_size: 1
schema: {mode: observed}
required_input_fields: [text]
response_field: answer
queries:
  - name: extract
    input_fields: {text: text}
    template: "Return the score stated in {{ row.text }}."
    response_format: structured
    output_fields:
      - {suffix: score, type: integer}
  - name: explain
    input_fields: {score: extract_score, text: text}
    template: "Explain score {{ row.score }} using {{ row.text }}."
```

`extract_score` is available to the second query because it is explicitly bound
in `input_fields`. Do not add generated fields to `required_input_fields` or
the node's input schema. Self/forward references and generated-name collisions
are rejected. Structured outputs support `string`, `integer`, `number`,
`boolean`, and `enum` (with `values`); nullable structured outputs are unsupported.

Every query still calls the provider. The node publishes one complete row only
after all queries succeed; a terminal failure routes the original entry row
through `on_error`. Earlier calls remain in the audit and can still incur
charges. There is no durable mid-node resume: recovery can repeat earlier calls.
`row.source_row` and image inputs continue to read the original entry row.
See [Ordered LLM queries](ordered-llm-queries.md) for the dependency and recovery
contract, and [multi-query configuration](../reference/configuration.md#multi-query-queries)
for output naming and typed response details.

### Keep secrets outside version-controlled YAML

`${OPENROUTER_API_KEY}` expands from the environment. The CLI can load an
explicit environment file with `elspeth --env-file .env validate --settings
settings.yaml`; global options go before the subcommand. Existing exported
values take precedence. Keep `.env` out of version control. `${VAR:-default}`
is available for optional non-secret values; missing required variables fail
configuration loading.

An executed configuration containing credentials needs the operator's
`ELSPETH_FINGERPRINT_KEY` so the audit stores secret fingerprints rather than
raw values. Do not disable secret protection to make an example run. For Key
Vault, profiles, and provider-specific prerequisites use
[Environment Variables](../reference/environment-variables.md),
[Secrets Settings](../reference/configuration.md#secrets-settings), and the
[Key Vault runbook](../runbooks/configure-keyvault-secrets.md).

Validation resolves configuration, may load Key Vault secrets, and constructs
plugins. It is not guaranteed to be a network-free operation for arbitrary
service plugins. A successful `validate` or `--dry-run` establishes configuration
and graph admission; it does not qualify credentials, model behavior, network
access, live effects, or downstream data. Only execute service examples after
their prerequisites and costs have been agreed. No provider call is required
to read or adapt the YAML fragments here.

## Inspect the audit and recover eligible runs

The walkthrough keeps its SQLite audit in `runs/audit.db`. If the SQLite CLI
is installed, inspect the run and locate an engine row ID:

```bash
cd "$DEMO_DIR"
sqlite3 runs/audit.db 'SELECT run_id, status FROM runs ORDER BY started_at DESC;'
sqlite3 runs/audit.db 'SELECT row_id, row_index FROM rows ORDER BY row_index;'
```

Use an actual `row_id` from the second query, not the input CSV's `id` value:

```bash
elspeth explain --run latest --row <ROW_ID> --database runs/audit.db --settings settings.yaml --no-tui
elspeth explain --run latest --row <ROW_ID> --database runs/audit.db --settings settings.yaml --json
```

`--settings` supplies the configured payload-store location for row content.
Without `--no-tui` or `--json`, `explain` opens the interactive lineage explorer.
Forked rows can have several terminal tokens; use `--token <TOKEN_ID>` to select
the particular branch. See [Explaining Pipeline Results](user-manual.md#explaining-pipeline-results).

For an interrupted/failed run, retain the original audit database, payload store,
checkpoint state, and compatible settings. Check eligibility before executing:

```bash
elspeth resume <RUN_ID> --settings settings.yaml --database runs/audit.db
elspeth resume <RUN_ID> --settings settings.yaml --database runs/audit.db --execute
```

There is no `resume --dry-run` flag: omitting `--execute` performs the check.
Ordinary streaming recovery refuses while unread source rows may remain.
Before starting a finite, single-source `csv` or `json` live pipeline, you can
set source `options.snapshot_for_resume: true` to retain its validated emissions
before downstream work. This has capture-time and memory costs and a 64 MiB
serialized spool limit; it is not supported for multiple sources. A snapshot
must finish sealing before it can support recovery.

Resume also checks topology, retained bytes, source lifecycle, and sink-effect
safety. It does not replay rows already terminally routed to an error sink.
Do not modify a failed run's settings to bypass a refusal. See
[Resume Failed Run](../runbooks/resume-failed-run.md) for the supported boundaries,
including non-GET web requests and uncertain external writes.

## Fix common authoring errors

| Symptom | What to check or change |
| --- | --- |
| YAML syntax error | Use spaces for indentation, align list items, close quotes/brackets, and quote expression strings and boolean route labels. |
| Missing/extra configuration field | Check the node envelope against `plugins inspect` and the configuration reference. Keep routing outside `options`; use plural `sources` with named entries. |
| Unknown plugin or missing optional dependency | Run `plugins list` in the same activated environment. Select a registered plugin or ask the operator to install its documented optional extra. |
| Unresolved connection, ambiguous producer, or unreachable node | Match upstream destination to downstream `input`; declare a queue for fan-in and a gate for forks. Do not rely on YAML list order. |
| Schema compatibility or missing guaranteed field | Use the normalized field name; make the actual producer guarantee it or correct the consumer's read. Do not invent source fields or empty required-input lists to silence validation. |
| Every LLM row fails template rendering | Use `row.text` for a declared single-query field; for multi-query use `row.<input_fields alias>`. Validate the template and check its input bindings. |
| Generated field required as upstream input | In ordered queries bind earlier output through `input_fields`; declare only real upstream columns in `required_input_fields`. Otherwise use a separate downstream LLM node. |
| Invalid route/result | Boolean expressions need string `"true"`/`"false"` labels; string expressions need matching labels for every result. Replace `continue` with a named destination. |
| Incomplete fork or expansion | Check every branch/member reaches its bound closer. Choose coalesce arrival or scope completeness policy explicitly; a collector accepts neither triggers nor `on_error`. |
| Row-union aggregation rejects an early trigger | Use `trigger: {}` or omit it so the union's correlated group cannot split at a batch boundary. |
| File not found or output in an unexpected directory | Check the process working directory and distinguish data paths from settings-relative template/reference paths. |
| Output target already exists | Choose a new path/fresh working directory, or deliberately use `auto_increment`; do not delete a previous run's output to silence the error. |
| Missing environment variable or fingerprint key | Supply the operator-approved environment configuration; keep credentials out of YAML and source control. |
| Landscape schema compatibility refusal | Use the operator's supported store/version procedure. Do not reset, downgrade, or edit a shared audit database to make a new configuration run. |
| `source lifecycle is incomplete` on resume | Recovery cannot assume unread rows do not exist. Follow the resume runbook; enable a supported snapshot before a future run if needed. |

Keep the settings, input/reference/template versions, output artifacts, and
audit retention policy together when promoting a pipeline. Validate each edit,
run a small representative local fixture where possible, then separately
qualify the real services and data your production run will use.
