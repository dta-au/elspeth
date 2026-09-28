# ELSPETH User Manual

This manual covers day-to-day use of the ELSPETH CLI and Web Composer for
building and running auditable pipelines.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Environment Configuration](#environment-configuration)
3. [CLI Commands](#cli-commands)
4. [Running Pipelines](#running-pipelines)
5. [Viewing Available Plugins](#viewing-available-plugins)
6. [Explaining Pipeline Results](#explaining-pipeline-results)
7. [Managing Storage](#managing-storage)
8. [Resuming Failed Runs](#resuming-failed-runs)
9. [Health Checks](#health-checks)
10. [Examples](#examples-walkthrough)
11. [Web Composer](#web-composer)

---

## Getting Started

### Installation

Install Python 3.12 or newer and `uv`, then synchronize the locked source
checkout:

```bash
git clone https://github.com/dta-au/elspeth.git
cd elspeth
uv sync --frozen --all-extras
source .venv/bin/activate
```

### Verify Installation

```bash
elspeth --version
elspeth --help
```

---

## Environment Configuration

See [Environment Variables Reference](../reference/environment-variables.md) for the complete list of supported variables, including LLM provider keys, Azure service credentials, and security settings.

**Quick start:** Create a `.env` file if you want local environment-based configuration, then fill in the required keys. ELSPETH automatically loads `.env` files from the current or parent directories.

---

## CLI Commands

### Global Options

```bash
elspeth [OPTIONS] COMMAND [ARGS]

Options:
  --version, -V    Show version and exit
  --no-dotenv      Skip loading .env file
  --env-file PATH  Path to .env file (skips automatic search)
  --verbose, -v    Enable verbose/debug logging
  --json-logs      Output structured JSON logs (for machine processing)
  --install-completion  Install completion for the current shell
  --show-completion     Show completion script for the current shell
  --help           Show help message
```

### Available Commands

| Command | Description |
|---------|-------------|
| `run` | Execute a pipeline |
| `validate` | Validate configuration without running |
| `explain` | Explain lineage for a row or token |
| `plugins list` | List available plugins |
| `purge` | Delete old payloads to free storage |
| `resume` | Resume a failed run from checkpoint |
| `export-resume` | Resume a finalized run's unfinished audit export |
| `join` | Attach to a running pipeline as a follower worker |
| `abandon` | Finalize a run whose leader died, recording its undecided work as abandoned |
| `health` | Check system health for deployment verification |
| `web` | Start the web application server |
| `composer users` | Add, remove, and bootstrap local Composer web users |
| `doctor` | Deployment readiness checks (`deployment`, `aws-ecs`) |

---

## Running Pipelines

### Validate First

Always validate your configuration before running:

```bash
elspeth validate --settings settings.yaml
```

Output shows:
- Source plugin and configuration
- Number of transforms
- Configured sinks
- Graph structure (nodes and edges)

### Execute a Pipeline

```bash
# Dry run - show what would happen
elspeth run --settings settings.yaml --dry-run

# Actually execute (requires explicit --execute flag)
elspeth run --settings settings.yaml --execute

# With verbose output
elspeth run --settings settings.yaml --execute --verbose

# JSON output (for machine processing)
elspeth run --settings settings.yaml --execute --format json
```

### Run Output

Console mode prints one bracketed line per phase, streaming progress lines, and
a single summary line. From
`elspeth run --settings examples/boolean_routing/settings.yaml --execute`:

```
[DATABASE] Connecting...
[DATABASE] ✓ Completed in 0.17s
[GRAPH] Building...
[GRAPH] ✓ Completed in 0.00s
[SOURCE] Initializing → csv...
[SOURCE] ✓ Completed in 0.00s
[PROCESS] Processing...
  Processing: 1 rows | 53 rows/sec | ✓1 ✗0 ⚠0 ↪1 ↯0
  Processing: 10 rows | 27 rows/sec | ✓10 ✗0 ⚠0 ↪10 ↯0
[PROCESS] ✓ Completed in 0.37s

✓ Run COMPLETED: 10 rows processed | ✓10 succeeded | ✗0 failed | ⚠0 quarantined | →10 routed (rejected:5, approved:5) | 0.39s total
```

The `→N routed` clause appears only when the run routed rows; the destination
order and the elapsed times vary per run.

Console mode does not print the run ID. To capture it for querying the audit
trail later, use `--format json` — the `run_completed` and `execution_result`
events both carry `run_id` — or query the most recent run with
`elspeth explain --run latest`.

### Exit Codes

`elspeth run --execute` (and `elspeth resume --execute`) exit with the
engine's completion taxonomy, so scripts and CI wrappers can branch on the
result:

| Exit code | Meaning |
|-----------|---------|
| 0 | Completed successfully — every row reached a clean outcome. Also returned for a run whose source yielded zero rows. |
| 1 | Completed with failures — at least one row failed or was quarantined (including a run where every row was quarantined). |
| 2 | Failed — no row reached success or quarantine. |
| 3 | Interrupted or evicted before completion. |
| 4 | Framework or audit-integrity error. |

Rows a source drops via a configured `on_validation_failure: discard` never
enter the pipeline: the validation error is still recorded in the audit
trail, but the run exits `0` when every ingested row succeeds. A transform's
`on_error: discard` is different — the dropped row is recorded as a
quarantined outcome, so the run reports completed-with-failures (exit `1`).

---

## Viewing Available Plugins

### List All Plugins

```bash
elspeth plugins list
```

Output:
```
SOURCES:
  aws_s3               - Load bounded CSV, JSON-array, or JSONL rows from one immutable S3 object.
  azure_blob           - Load rows from Azure Blob Storage.
  blob_rows            - Emit one five-field custody row per configured managed blob.
  csv                  - Load rows from a CSV file.
  dataverse            - Load rows from Microsoft Dataverse via OData v4 REST API.
  json                 - Load rows from a JSON file.
  null                 - A source that yields no rows.
  text                 - Load one output row per text line into a configured column.
  llm                  - Issue one authored prompt and emit at most one validated source row.

TRANSFORMS:
  batch_classifier_metrics - Compute classifier confusion matrix and F-score metrics over a batch.
  batch_data_quality_report - Report field-level batch quality counts and rates.
  batch_distribution_profile - Compute distribution summaries over aggregation batches.
  batch_drift_compare  - Compare baseline and current cohort distributions over a batch.
  batch_effect_size    - Compute Cohen's d and Hedges' g for batch variant comparisons.
  batch_experiment_compare - Compare experiment variants over a batch using mean deltas.
  batch_outlier_annotator - Annotate batch rows with z-score and robust-z outlier signals.
  batch_paired_preference - Compare paired variant scores over an aggregation batch.
  batch_rank           - Rank every row of a batch by a numeric field; one output row per input row, in order.
  batch_replicate      - Replicate rows based on a copies field.
  batch_stats          - Compute aggregate statistics over a batch of rows.
  batch_threshold_summary - Report threshold match counts and rates for finite numeric batch values.
  batch_top_k          - Report most frequent scalar values over a batch.
  blob_csv_expand      - Parse a CSV blob and emit one output row per CSV data row.
  blob_fetch           - Fetch an HTTP(S) URL into the run payload store and emit a blob reference.
  blob_json_expand     - Parse a JSON document and emit one output row per record.
  blob_text_expand     - Decode a text blob from the payload store and emit one row per line or chunk.
  field_mapper         - Map, rename, and select row fields.
  json_explode         - Explode a JSON array field into multiple rows.
  keyword_filter       - Filter rows containing blocked content patterns.
  line_explode         - Explode a string field into one output row per line.
  passthrough          - Pass rows through unchanged.
  pdf_rasterize        - Render each page of a PDF into a PNG payload and emit one row per page.
  reference_join       - Match a row field against a reference table and add named fields to the row.
  report_assemble      - Assemble a paginated report from a flushed batch of text rows.
  truncate             - Truncate string fields to specified maximum lengths.
  type_coerce          - Perform explicit, strict, per-field type normalization.
  value_transform      - Apply expressions to compute new or modified field values.
  web_scrape           - Fetch webpages, extract content, generate fingerprints.
  aws_bedrock_content_safety - Block configured harmful-content categories through Bedrock Guardrails.
  aws_bedrock_prompt_shield - Block prompt attacks identified by an operator-owned Guardrail.
  aws_textract_document_analysis - Enrich S3 document references through asynchronous Amazon Textract analysis.
  aws_textract_inline_analysis - Enrich managed-blob document rows through synchronous Amazon Textract analysis.
  azure_ai_search      - RAG retrieval against an existing Azure AI Search index.
  azure_content_safety - Analyze content using Azure Content Safety API.
  azure_document_intelligence - Enrich rows with Azure Document Intelligence extraction (async analyze LRO).
  azure_prompt_shield  - Detect jailbreak attempts and prompt injection using Azure Prompt Shield.
  llm                  - Unified LLM transform with provider dispatch and strategy selection.
  rag_retrieval        - Enriches rows with retrieval-augmented context from search providers.

SINKS:
  aws_s3               - Write bounded cumulative CSV, JSON, or JSONL objects to AWS S3.
  azure_blob           - Write rows to Azure Blob Storage.
  chroma_sink          - Write pipeline rows into a ChromaDB collection.
  csv                  - Write rows to a CSV file.
  database             - Write rows to a database table.
  dataverse            - Write rows to Microsoft Dataverse via OData v4 REST API.
  document             - Write one configured field's whole value to a file, byte-for-byte.
  json                 - Write rows to a JSON file.
  text                 - Write one configured string field per canonical LF-delimited record.
```

### Filter by Type

```bash
elspeth plugins list --type source
elspeth plugins list --type transform
elspeth plugins list --type sink
```

### Machine-Readable Catalog

```bash
elspeth plugins list --format json
elspeth plugins list --type source --format json
elspeth plugins inspect source csv
elspeth plugins inspect source csv --format json
```

`plugins inspect` shows the catalog description, config fields, JSON Schema,
and composer knob schema for one plugin.

---

## Explaining Pipeline Results

### Query by Run ID

```bash
# Explain the latest run
elspeth explain --run latest --database <path/to/audit.db>

# Explain a specific run
elspeth explain --run e58480edd52a4292809928bd6425f4ed --database <path/to/audit.db>
```

### Query Specific Rows

```bash
# Explain a specific row
elspeth explain --run latest --row 42 --database <path/to/audit.db>

# Explain by token ID (for forked rows)
elspeth explain --run latest --token abc123 --database <path/to/audit.db>
```

### Output Formats

```bash
# Interactive TUI (default)
elspeth explain --run latest --database <path/to/audit.db>

# Plain text (for non-interactive terminals or CI/CD)
elspeth explain --run latest --no-tui --database <path/to/audit.db>

# JSON output
elspeth explain --run latest --json --database <path/to/audit.db>

# Disambiguate when a row has multiple terminal tokens (e.g., forked rows)
elspeth explain --run latest --row 42 --sink high_values --database <path/to/audit.db>
```

The interactive TUI shows a selectable lineage tree and detail panel. Use arrow
keys to move through run, branch, node, token, and status rows; press Enter to
update the detail panel; press `r` to refresh and `q` to quit. Use `--row`,
`--token`, and `--sink` to focus the initial lineage view. In non-interactive
terminals or CI, prefer `--no-tui` or `--json`.

---

## Managing Storage

### Purge Old Payloads

Over time, payload storage grows. Purge old data while preserving audit metadata:

```bash
# See what would be deleted (dry run)
elspeth purge --dry-run --retention-days 90

# Actually delete (with confirmation)
elspeth purge --retention-days 90

# Skip confirmation prompt
elspeth purge --retention-days 90 --yes

# Specify database and payload directory explicitly
elspeth purge --database ./runs/audit.db --payload-dir ./runs/payloads --retention-days 30
```

**Note:** Purging deletes payload blobs but preserves hashes in the audit trail. You can still verify what data existed, you just can't retrieve the content.

---

## Resuming Failed Runs

If a run fails (e.g., API timeout, network error), you can resume from the last checkpoint:

### Check Resume Status

```bash
# Dry run - show resume information (positional run_id argument)
elspeth resume run-abc123 --settings settings.yaml --database ./runs/audit.db

Output:
  Run run-abc123 can be resumed.

  Resume point:
    Sequence number: 45
    Has barrier scalars: No
    Blocked barrier rows (journal): 0
    Scheduler work items (to re-drive): 55

  Dry run - use --execute to actually resume processing.
  Topology validation passed - checkpoint is compatible with current config.
```

### Execute Resume

```bash
elspeth resume run-abc123 --execute --settings settings.yaml --database ./runs/audit.db

# JSON output
elspeth resume run-abc123 --execute --format json
```

Resume mode:
- Uses `NullSource` (data comes from stored payloads)
- Appends to existing output files (doesn't overwrite)
- Continues from last successful checkpoint
- Exits with the same [exit codes](#exit-codes) as `elspeth run --execute`

### Leaderless Runs (`abandon`)

In a multi-worker pack (`elspeth run` leader plus `elspeth join` followers),
a leader that is killed mid-run leaves the run `running` with an expired
seat. Followers notice the dead seat and exit 2. `elspeth resume` can take
the seat over, but it refuses while any source is still `loading`: resume
replays only the rows that were persisted, so it cannot prove that no unread
source rows exist. Because a source is recorded `exhausted` only after its
last row has finished processing, that refusal covers almost the whole life
of a run.

`elspeth abandon` is the way out. It takes the dead leader's seat through the
same takeover CAS resume uses and finalizes the run as `interrupted` under
that seat: every token nothing will ever decide is recorded as `abandoned`
(ADR-038), open sink effects are failed, followers are departed, and the
seat is vacated. The abandoned work stays in the audit trail; reprocess the
source with a fresh run.

```bash
# Dry run - show the leaderless state and whether resume would work instead
elspeth abandon run-abc123 --settings settings.yaml --database ./runs/audit.db

Output:
  Run run-abc123
    Status: running
    Leader seat: worker:run-abc123:1f3a… (expired 2026-09-08 03:12:44+00:00)
    Sources: primary=loading
    Scheduler work items: leased=1, pending_sink=119, terminal=1
    Undecided tokens: 120
    Resumable: no — this run cannot be resumed: source lifecycle is incomplete (primary=loading) …

  Dry run - use --execute to take the dead leader's seat and finalize the run as interrupted.
    120 undecided token(s) would be recorded as abandoned (ADR-038).

# Take the seat and finalize
elspeth abandon run-abc123 --settings settings.yaml --database ./runs/audit.db --execute
```

`abandon` refuses (exit 1) when the run does not exist, is already terminal,
or is led by a live seat — a live-led run should be joined, not abandoned.
When the dry run reports `Resumable: yes`, prefer `elspeth resume`; abandoning
a resumable run finalizes it as `interrupted` without abandoning any token,
and it stays resumable.

---

## Health Checks

The `health` command verifies system readiness for deployment:

```bash
# Basic health check
elspeth health

# Verbose output with details
elspeth health --verbose

# JSON output (for automation)
elspeth health --json
```

### Health Check Options

| Option | Description |
|--------|-------------|
| `--verbose, -v` | Include detailed check information |
| `--json, -j` | Output as JSON |
| `--host TEXT` | Web server host to check. Defaults to `ELSPETH_WEB__HOST` or `127.0.0.1` |
| `--port, -p INTEGER` | Web server port to check. Defaults to `ELSPETH_WEB__PORT` or `8451` |
| `--skip-web / --check-web` | Skip the web interface check. Default: skip (batch containers). Use `--check-web` to probe |

### What Gets Checked

- **version**: ELSPETH version
- **commit**: Git commit SHA (if available)
- **python**: Python version
- **database**: Database connectivity (if `DATABASE_URL` is set)
- **config_dir**: Configuration directory
- **output_dir**: Output directory
- **plugins**: Plugin availability
- **web**: Web interface reachability (skipped unless `--check-web` is passed)

### Example JSON Output

```json
{
  "status": "healthy",
  "version": "0.8.0",
  "commit": "abc123f",
  "checks": {
    "version": {"status": "ok", "value": "0.8.0"},
    "commit": {"status": "ok", "value": "abc123f"},
    "python": {"status": "ok", "value": "3.13.1"},
    "database": {"status": "ok", "value": "connected"},
    "config_dir": {"status": "ok", "value": "./config"},
    "output_dir": {"status": "ok", "value": "./output"},
    "plugins": {"status": "ok", "value": "9 sources, 37 transforms, 9 sinks"},
    "web": {"status": "skip", "value": "skipped via --skip-web"}
  }
}
```

---

## Examples Walkthrough

ELSPETH includes several example pipelines in `examples/`:

### 1. Boolean Routing

Routes rows based on a true/false field.

```bash
elspeth run -s examples/boolean_routing/settings.yaml --execute
```

**Input:** CSV with `approved` column (true/false)
**Output:** Separate CSVs for approved and rejected rows

**Verify:**
```bash
wc -l examples/boolean_routing/output/*.csv
#   6 approved.csv   (5 data rows + header)
#   6 rejected.csv   (5 data rows + header)
```

### 2. Threshold Gate

Routes high-value transactions to separate output.

```bash
elspeth run -s examples/threshold_gate/settings.yaml --execute
```

**Input:** CSV with `amount` column
**Output:** High values (>1000) and normal values in separate files

**Verify:**
```bash
cat examples/threshold_gate/output/high_values.csv | head -3
# id,amount,description
# 2,1500,Large purchase
# 4,2000,Premium service
```

### 3. Batch Aggregation

Computes statistics over batches of rows.

```bash
elspeth run -s examples/batch_aggregation/settings.yaml --execute
```

**Input:** 15 transactions
**Output:** 3 batch summaries (one per 5 rows, grouped by category)

**Verify:**
```bash
cat examples/batch_aggregation/output/batch_summaries.csv
# category,count,sum,mean
# electronics,5,2750,550.0
# clothing,5,1250,250.0
# groceries,5,375,75.0
```

### 4. Deaggregation

Demonstrates N→M row expansion with new tokens.

```bash
elspeth run -s examples/deaggregation/settings.yaml --execute
```

**Input:** 6 rows with `copies` field (values: 2,1,3,2,1,2 = 11 total)
**Output:** 11 rows (each replicated by its copies value)

**Verify:**
```bash
wc -l examples/deaggregation/output/replicated.csv
# 12 (11 data rows + header)

head -4 examples/deaggregation/output/replicated.csv
# id,name,copies,category,copy_index
# 1,Alice,2,standard,0
# 1,Alice,2,standard,1
# 2,Bob,1,premium,0
```

### 5. JSON Explode

Expands array fields into individual rows.

```bash
elspeth run -s examples/json_explode/settings.yaml --execute
```

**Input:** 3 orders with `items` arrays
**Output:** 6 rows (one per item)

**Verify:**
```bash
cat examples/json_explode/output/order_items.json | head -20
# Shows individual items with order_id, item details, and item_index
```

### 6. Audit Export

Exports complete audit trail to JSON for compliance.

```bash
elspeth run -s examples/audit_export/settings.yaml --execute
```

**Input:** 8 submissions
**Output:** Routed results + complete audit trail JSON

**Verify:**
```bash
# Check routed outputs
wc -l examples/audit_export/output/*.csv
#   5 corporate.csv      (4 data rows + header)
#   5 non_corporate.csv  (4 data rows + header)

# Check audit trail exists and has content
ls -la examples/audit_export/output/audit_trail.json
# Should show non-zero file size
```

### Additional Examples

For more complex scenarios, see the configuration reference:

- **LLM Sentiment Analysis** - Using `llm` plugin (with provider: openrouter) and templates
- **Content Moderation with Routing** - Gates with condition expressions
- **Fork/Join Patterns** - Parallel processing with coalesce

See [Configuration Reference](../reference/configuration.md) for the complete settings documentation.

---

## Web Composer

The Web Composer is a browser-based authoring surface for building ELSPETH
pipelines without hand-editing YAML. Start it with:

```bash
elspeth web
```

Then open the URL printed on the console (typically <http://localhost:8451>).

### Using the desktop workspace

On desktop, the Composer keeps authoring and the current pipeline together in
one workspace. The authoring pane contains the conversation. The pipeline
workspace stays beside it so you can inspect the artifact without leaving
the conversation.

- Drag the divider to resize the authoring pane. You can also focus the divider
  and use Left/Right Arrow, Shift+Left/Right Arrow for larger steps, or Home/End
  for its minimum or maximum width.
- Select **Collapse authoring pane** when you want more room for the pipeline.
  **Restore authoring pane** reopens it; the collapsed control continues to
  report busy, error, or unread authoring status.
- Use **Graph**, **Spec**, **YAML**, **Checks**, and **Run** to switch the
  persistent pipeline view. Graph and Run are always available; Spec, YAML, and
  Checks become available once the pipeline has content, and YAML also appears
  while a YAML proposal is pending review. Spec summarizes its
  components and configuration, YAML provides the current export controls,
  Checks carries a status badge and renders validation and audit content
  inline, and Run shows current or recent execution results.
- Use **Focus Graph** for an optional full-screen graph. The persistent Graph
  tab remains the normal working view.

The workspace action bar keeps state-dependent actions such as **Save for
review** and **Run pipeline** reachable without scrolling the conversation.
**Import YAML** sits between them when the **Detail level** preference is set
to **Show technical detail**, which is not the default. Export is not on the
bar: the YAML tab's **Copy** and **Download** controls carry it. The plugin
catalog opens from the artifact workspace toolbar.

When the workspace is too narrow for both main panes, use the **Compose** and
**Pipeline** switcher to choose which pane is visible. Pane resizing is disabled
in this narrow layout.

Two global shortcuts select and focus persistent artifact tabs:

| Shortcut | Result |
| --- | --- |
| `Ctrl/Cmd+Shift+G` | Select Graph. |
| `Ctrl/Cmd+Shift+Y` | Select YAML when the pipeline has content. |

These shortcuts select the workspace tab; they do not open Focus Graph. Press
`?` outside a text field to see the complete keyboard-shortcut reference.

The saved pane width and collapsed state are local interface preferences. They
do not change the pipeline, session, validation, audit, or execution semantics.

New sessions use freeform conversation. Describe the pipeline you want in the
chat input, including the source, output, transformations, and any routing
constraints. The LLM proposes the structure; ELSPETH validates and records
applied changes. New sessions default to **Auto-apply on**: eligible changes
commit as audited pipeline versions without a separate Accept click. A
full-pipeline proposal auto-commits only after a green runtime preflight and
while the session remains in auto-apply mode. Otherwise it remains pending for
review. With **Approval required**, mutations wait as proposals for explicit
Accept or Reject. The authority chip in the chat header shows the current mode.
Follow-up messages can refine the draft without starting over. Committing a
pipeline change never executes it; **Run pipeline** is a separate action.

The first-run tutorial uses this same authoring path and a fixed example. It
continues through **Run**, **Audit**, and **Graduation** so the user sees a real
execution and its evidence before starting an ordinary session.

### Supported pipeline structures

Composer can author the full canonical set of pipeline structures:

- **Linear transform chains** (`linear_transform`) — a source through one or
  more transforms to a
  sink.
- **Conditional gates** (`conditional_gate`) — route rows down different paths
  by a condition.
- **Multiple outputs** (`multi_output`) — fan a stream out to several sinks,
  including a
  write-failure fallback to another output.
- **Fork and coalesce** (`fork_coalesce`) — split a stream into parallel branches
  and merge them back, including require-all union merges.
- **Fork and row union** (`row_union`) — wait for every correlated fork branch,
  then release the original rows unchanged in declared branch order for
  downstream processing.
- **Multi-source queue fan-in** (`multi_source_queue`) — several sources feeding
  one downstream queue.
- **Batch aggregation** (`aggregation`) — compute statistics over batches or
  groups.
- **Row expansion** (`row_expansion`) — expand one row into many (deaggregation,
  JSON explode).
- **Error routing** (`error_routing`) — send failed rows to a dedicated failure
  output.
- **Structured LLM output consumed downstream** (`structured_llm`) — a typed
  multi-field LLM result that later stages read by field.

Describe the entire requested shape at once, or start with a small pipeline and
ask for revisions in later turns. For example, you can add an LLM transform
after the source and sinks already exist; the model proposes the change against
the current composition and ELSPETH validates the result.

### Validation, interpretation, and sign-off

The LLM proposes changes, but it is not the authority. ELSPETH validates and
records the result, then shows a plain-language gloss, validation summary, and
graph impact. Depending on the authority mode and preflight result, that
result is either an applied pipeline version or a proposal awaiting review.

If a proposal depends on a subjective interpretation, Composer surfaces a
review card and blocks the affected action until it is resolved. An advisory
review may also withhold completion until the current graph has been reviewed.

### Completion and execution

Once a pipeline change commits, automatically or after explicit approval, you
can validate the pipeline, preview the YAML, and execute it directly from
Composer. The composer's `/validate` and
`/execute`
endpoints use the same runtime assembly and graph validation contracts as
`elspeth validate` and `elspeth run` — there is no separate UI-only validator.

### The first-run tutorial

The first-run tutorial supplies fixed sample data and a fixed task to the
ordinary freeform Composer. Its Build step uses the same planner, authority
mode, and validation as any other session. Continue through Run and Audit to
see the pipeline execute and inspect its evidence; Graduation then hands you
to ordinary authoring. The tutorial has no separate planner or reduced schema.

### See also

- For Composer authoring, plugin discovery, and tool contracts, see
  the composer skill at `src/elspeth/web/composer/skills/pipeline_composer.md`.

---

## Troubleshooting

For comprehensive troubleshooting, see the [Troubleshooting Guide](troubleshooting.md).

### Quick Fixes

**"ELSPETH_FINGERPRINT_KEY is not set"** - Set the key or allow raw secrets for development:
```bash
export ELSPETH_FINGERPRINT_KEY="$(openssl rand -hex 32)"
# OR for development only:
export ELSPETH_ALLOW_RAW_SECRETS=true
```

Persist the generated production fingerprint key in the deployment secret
manager; changing it breaks credential correlation across audit records.

**"Unknown plugin: xyz"** - Check available plugins with `elspeth plugins list` (names are case-sensitive).

**Pipeline hangs** - Run with `--verbose` to identify the bottleneck and check your rate limit configuration.

---

## Getting Help

```bash
# General help
elspeth --help

# Command-specific help
elspeth run --help
elspeth plugins --help
elspeth explain --help
```

For bug reports and feature requests, see the project repository. Include a
sanitized reproduction, not raw session data. Do not attach raw composer chat
history or session exports; remove secrets, tokens, PII, blob contents, sample
rows, URLs, and organization-specific identifiers before posting.
