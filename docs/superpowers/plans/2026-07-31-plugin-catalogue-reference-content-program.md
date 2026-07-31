# Plugin Catalogue Reference Content Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Plugin Catalogue's empty reference placeholders with accurate, validated “Use when”, “Avoid when”, example YAML, and discovery tags for all 47 registered built-in plugins, then make omissions a test failure.

**Architecture:** Reference content remains class-owned beside each plugin's executable contract. Family-specific tests validate content and YAML while parallel packages are in flight; a registry-driven integration test closes the program by discovering the live built-ins and enforcing completeness without a hand-maintained production manifest. The frontend stops offering a Details panel when a legacy or third-party plugin genuinely has no reference content, so the generic placeholder is removed without making optional protocol fields mandatory for external plugins.

**Tech Stack:** Python 3.13, ELSPETH plugin contracts and `PluginManager`, the bounded YAML loader, Pydantic plugin config models, pytest, React 18, TypeScript, Vitest/Testing Library, Playwright, `elspeth-lints`, Filigree.

---

## Program status and release boundary

This plan was prepared from live discovery on 2026-07-31 at `release/0.7.2` commit `4491d503`. The checkout registered:

- 7 sources;
- 32 transforms; and
- 8 sinks.

Three plugins currently have all four reference fields (`source:csv`, `transform:aws_textract_document_analysis`, and `transform:azure_document_intelligence`). `transform:value_transform` has only Avoid prose. Two batch transforms have only the `narrative-summary` tag. The other registered plugins inherit empty defaults.

Do not land this broad metadata change into the active 0.7.2 release train by default. Every edited plugin module changes `source_file_hash`, which changes release fingerprint inputs and can invalidate trust-tier judge staging. The preferred base is the first post-0.7.2 development checkpoint.

If the owner explicitly brings the work into 0.7.2, merge the complete program before judge staging. Then use the supported key-free `elspeth-judge` staging workflow and have the operator repeat signing and baseline generation. Agents must never access `ELSPETH_JUDGE_METADATA_HMAC_KEY`, run the operator command, or hand-edit judge signatures.

The current shared `.venv` resolves ELSPETH from a sibling worktree. Until that environment is rebuilt by its owner, every Python command in this plan intentionally uses:

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/python
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest
```

Do not run `uv pip install` from a worktree and do not mutate the shared `.venv` as part of this program.

## Scope decisions

1. **The live registry is the inventory authority.** Document all 47 registered built-ins. Do not create a second production manifest.
2. **`source:null` is documented, not silently exempted.** It remains tagged `internal`, badged as internal, and hidden from guided selection. Its Use/Avoid text must say that it is a resume-only zero-row placeholder. Its quoted YAML example prevents accidental YAML-null parsing and makes the limitation concrete.
3. **Gates and structural graph nodes are out of scope.** They are system operations, not registered plugins.
4. **Examples are bounded component fragments.** Sources use top-level `sources`, ordinary transforms use `transform`, batch-aware transforms use `aggregations`, and sinks use `sinks`. A fragment must contain exactly one occurrence of its declaring plugin.
5. **Credentials use secret-ref markers or operator profiles.** Examples must not contain credential literals. Test validation resolves an approved marker to a sentinel only after checking marker placement.
6. **No runtime behavior or policy changes ride with content packages.** Policy gaps found while researching a plugin become separately triaged Filigree issues.
7. **No plugin-version bump.** These are documentation-only class attributes. Recompute `source_file_hash` for every edited plugin module.
8. **The wire schema stays optional.** External and legacy plugin compatibility is preserved. Completeness is required for registered built-ins by tests.
9. **Details remain collapsed by default.** The frontend change only removes the misleading generic fallback and treats null, empty, and whitespace-only fields consistently.

## Authoring contract

Every registered built-in must define:

- `usage_when_to_use`: at least one concrete input/workflow shape, the useful outcome, and any decisive operating context;
- `usage_when_not_to_use`: at least one hard limitation or unsafe fit and a concrete alternative where one exists;
- `example_use`: one parseable YAML component fragment using only real options and a realistic, non-secret value;
- `capability_tags`: a tuple of 2–6 unique lowercase kebab-case discovery terms.

Content must also:

- use present-tense behavior from the live implementation, not roadmap behavior;
- distinguish Web Composer authority, operator-profiled configuration, and CLI/batch-only use when that distinction matters;
- state boundedness, resume/append behavior, external-call retention, failure routing, or aggregation-window semantics when those facts affect selection;
- avoid generic duplicates of the technical description;
- avoid invented plugin names, options, credentials, endpoints presented as deployment facts, and claims of statistical significance that the plugin does not compute;
- preserve `narrative-summary` on `batch_classifier_metrics` and `batch_distribution_profile`; and
- keep YAML short enough to read in the collapsed catalogue while still validating against the owning config model.

## Dependency and merge shape

```text
WP0 contract + test kit
  ├── WP1 sources (7)
  ├── WP2 sinks (8)
  ├── WP3 core transforms (10)
  ├── WP4 batch transforms (12)
  └── WP5 external/provider transforms (10)
          ↓
WP6 frontend fallback removal
          ↓
WP7 registry gate + API parity + integrated verification
```

WP1–WP5 may run in parallel after WP0 is merged. Each package owns only its listed plugin modules and its family test. Merge each green package into one integration branch; do not make every worker edit a shared inventory test. WP6 is independent of the content files. WP7 owns all shared closeout tests and lands last.

Recommended worktrees:

```bash
git fetch origin
CATALOGUE_BASE="$(git rev-parse origin/main)"
git merge-base --is-ancestor origin/release/0.7.2 "$CATALOGUE_BASE"
git worktree add .claude/worktrees/catalog-sources -b codex/catalog-sources "$CATALOGUE_BASE"
git worktree add .claude/worktrees/catalog-sinks -b codex/catalog-sinks "$CATALOGUE_BASE"
git worktree add .claude/worktrees/catalog-core-transforms -b codex/catalog-core-transforms "$CATALOGUE_BASE"
git worktree add .claude/worktrees/catalog-batch-transforms -b codex/catalog-batch-transforms "$CATALOGUE_BASE"
git worktree add .claude/worktrees/catalog-provider-transforms -b codex/catalog-provider-transforms "$CATALOGUE_BASE"
```

The ancestry check must return exit 0. If it does not, the post-release checkpoint has not reached `origin/main`; stop rather than broadening the release branch.

Symlink each worktree's `.venv` to `/home/john/elspeth/.venv`, then use the explicit `PYTHONPATH` commands above. Rebase the five packages after WP0 so they share the test kit.

## File structure

### Shared contract and tests

- Create `docs/contracts/plugin-catalogue-reference-content.md` — durable field semantics, example shapes, secret handling, tags, and author checklist.
- Create `tests/fixtures/catalog_reference.py` — bounded fragment parsing, plugin-node lookup, secret-marker normalization, config validation, and content assertions.
- Create `tests/unit/plugins/test_catalog_reference_testkit.py` — focused tests for the helper's accept/reject behavior.
- Create `tests/unit/plugins/test_catalog_reference_content.py` — final registry-driven completeness gate.
- Modify `src/elspeth/plugins/infrastructure/base.py` — replace the deleted design-document link with the durable contract.
- Modify `src/elspeth/web/catalog/schemas.py` — explain optional wire compatibility versus built-in completeness.
- Modify `tests/unit/web/catalog/test_service.py` — prove metadata is serialized unchanged.
- Modify `tests/unit/web/catalog/test_routes.py` — prove source, transform, and sink endpoints preserve reference fields.

### Frontend

- Modify `src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx` — trim-aware field presence and no generic fallback.
- Modify `src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx` — absent, whitespace-only, partial, and complete Details behavior.
- Modify the existing catalogue Playwright specification under `src/elspeth/web/frontend/tests/e2e/` selected in Task 8 — representative source/transform/sink disclosure coverage.

### Family tests

- Create `tests/unit/plugins/sources/test_source_catalogue_metadata.py`.
- Create `tests/unit/plugins/sinks/test_sink_catalogue_metadata.py`.
- Create `tests/unit/plugins/transforms/test_core_catalogue_metadata.py`.
- Create `tests/unit/plugins/transforms/test_batch_catalogue_metadata.py`.
- Create `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`.

---

### Task 1: Establish the durable reference-content contract and reusable test kit

**Files:**

- Create: `docs/contracts/plugin-catalogue-reference-content.md`
- Create: `tests/fixtures/catalog_reference.py`
- Create: `tests/unit/plugins/test_catalog_reference_testkit.py`
- Modify: `src/elspeth/plugins/infrastructure/base.py`
- Modify: `src/elspeth/web/catalog/schemas.py`

- [ ] **Step 1: Write failing test-kit tests**

Cover:

- null, empty, whitespace-only, generic, and duplicated prose;
- tags that are a list, duplicated, uppercase, blank, generic-only, or longer than 32 characters;
- a valid source fragment under `sources`;
- an invalid deleted singular `source` fragment;
- a valid ordinary `transform` fragment;
- a valid batch-aware fragment under `aggregations`;
- a valid sink fragment under `sinks`;
- zero or two occurrences of the declaring plugin;
- a wrong plugin ID;
- unknown options rejected by the owning config model;
- an allowed `{secret_ref: NAME}` marker normalized only for config validation;
- a secret ref in a disallowed field;
- a literal credential value; and
- quoted `plugin: "null"` with no options and `config_model is None`.

The helper API must be small and family-test friendly. Define a frozen `BuiltinReference` dataclass with a `Literal["source", "transform", "sink"]` kind and a plugin-class field typed as the union of `type[BaseSource]`, `type[BaseTransform]`, and `type[BaseSink]`. Export `discover_builtin_references`, `assert_reference_text`, `assert_reference_tags`, and `parse_and_validate_example`.

Use `PluginManager().register_builtin_plugins()` for discovery and `load_bounded_pipeline_yaml` for parsing. Recursively locate dictionaries whose `plugin` equals `plugin_cls.name`; require exactly one. Call `plugin_cls.get_config_model(resolved_options)` and then `from_dict(resolved_options, plugin_name=plugin_cls.name)` without constructing the plugin or making an external call.

- [ ] **Step 2: Run the new tests and verify they fail because the helper does not exist**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/test_catalog_reference_testkit.py -q
```

Expected: import/collection failure for `tests.fixtures.catalog_reference`.

- [ ] **Step 3: Implement the helper**

Use `collect_credential_field_violations` and `collect_disallowed_secret_ref_markers` on the unmodified options. Recursively replace an exact mapping shaped like `{"secret_ref": "<nonblank name>"}` with a fixed non-secret sentinel only after those checks pass. Do not resolve environment variables or read the process environment.

Infer the required YAML shape as follows:

```python
if reference.kind == "source":
    required_top_level = "sources"
elif reference.kind == "sink":
    required_top_level = "sinks"
elif bool(getattr(reference.plugin_cls, "is_batch_aware", False)):
    required_top_level = "aggregations"
else:
    required_top_level = "transform"
```

For `sources`, `sinks`, and `aggregations`, accept the repository's mapping or list form after confirming the one declaring-plugin node. Do not require a complete runnable pipeline; routing validation belongs to settings tests, not a component-fragment catalogue test.

- [ ] **Step 4: Write the durable contract**

Copy the Scope decisions and Authoring contract from this plan into `docs/contracts/plugin-catalogue-reference-content.md`, then add:

- one valid source fragment;
- one ordinary transform fragment;
- one aggregation fragment;
- one sink fragment;
- the secret-ref marker convention;
- the hash-refresh command;
- the focused/global test commands; and
- a checklist telling future plugin authors that the registry-driven gate will fail until all four fields are present.

Do not copy program scheduling, worktree instructions, or release-specific facts into the durable contract.

- [ ] **Step 5: Point code comments at the durable contract**

In `base.py`, remove the stale `docs/composer/ux-redesign-2026-05/08-catalog-reshape.md` reference and the claim that the UI always renders a fallback. Describe examples as bounded YAML component fragments, not “one-or-two-line” snippets and not singular `source:`.

In `schemas.py`, retain optional response fields but explain that registered built-ins are complete by repository test while third-party/legacy entries may omit them.

- [ ] **Step 6: Run focused tests**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/test_catalog_reference_testkit.py \
  tests/unit/plugins/infrastructure/test_base_metadata.py -q
git diff --check
```

Expected: PASS.

- [ ] **Step 7: Commit WP0**

```bash
git add \
  docs/contracts/plugin-catalogue-reference-content.md \
  tests/fixtures/catalog_reference.py \
  tests/unit/plugins/test_catalog_reference_testkit.py \
  src/elspeth/plugins/infrastructure/base.py \
  src/elspeth/web/catalog/schemas.py
git diff --cached --check
git commit -m "test: define plugin catalogue content contract"
```

---

### Task 2: Document all seven sources

**Files:**

- Modify: `src/elspeth/plugins/sources/aws_s3_source.py`
- Modify: `src/elspeth/plugins/sources/azure_blob_source.py`
- Modify: `src/elspeth/plugins/sources/csv_source.py`
- Modify: `src/elspeth/plugins/sources/dataverse.py`
- Modify: `src/elspeth/plugins/sources/json_source.py`
- Modify: `src/elspeth/plugins/sources/null_source.py`
- Modify: `src/elspeth/plugins/sources/text_source.py`
- Create: `tests/unit/plugins/sources/test_source_catalogue_metadata.py`
- Modify: `tests/unit/plugins/sources/test_csv_source_metadata.py`

The source package must encode these exact selection facts:

| Plugin | Use when | Avoid when / boundary | Example basis | Tags |
|---|---|---|---|---|
| `source:aws_s3` | Trusted CLI/batch ingestion of one bounded CSV, JSON-array, or JSONL object using the AWS default chain and ETag pinning | Ordinary Web Composer, streams, prefixes, multi-object ingestion, or YAML credentials | `sources.s3_input`, bucket, key, `format: jsonl`, observed schema, validation discard | `aws`, `s3`, `object-storage`, `batch` |
| `source:azure_blob` | One approved Azure blob containing CSV, JSON-array, or JSONL | Composer uploads, prefixes/containers/streams, or unbounded whole-object materialization | Managed identity, one container/blob path, JSONL, observed schema | `azure`, `blob-storage`, `object-storage`, `batch` |
| `source:csv` | Finite tabular file, boundary coercion, optional malformed-row quarantine, incremental record emission | Inline data, unbounded/live streams, or direct HTTP arrival | Replace deleted `source:` with `sources.primary`; include required `on_success`, schema, and validation policy | `csv`, `file`, `batch`, `tabular` |
| `source:dataverse` | Approved Dataverse environment with entity/OData or FetchXML and paginated audited reads | Writes, webhooks/change streams, invented tenant/entity/query facts, or local uploads | Managed identity, entity plus `select`, observed schema | `microsoft`, `dataverse`, `odata`, `fetchxml`, `batch` |
| `source:json` | JSON array or JSONL records; `data_key` only for wrapped JSON arrays | Plain text/CSV; very large array documents; `data_key` with JSONL | JSONL file with observed schema and validation policy | `json`, `jsonl`, `file`, `batch` |
| `source:null` | Resume graph reconstruction only; emits zero rows | Any new ingestion or placeholder-data use | `plugin: "null"` under `sources.resume_placeholder`; no options | `internal`, `resume`, `placeholder` |
| `source:text` | One retained text/Markdown line per record in one named field | Structured/multi-field input or whole-document preservation | Text path, `column: url`, observed schema | `text`, `file`, `line-oriented`, `batch` |

- [ ] **Step 1: Write the failing family test**

Discover sources through the test kit and assert the exact identity set:

```python
{
    "aws_s3", "azure_blob", "csv", "dataverse", "json", "null", "text"
}
```

Run all four helper validations for every source. Add explicit assertions that:

- only `source:null` has the `internal` tag;
- its example contains `plugin: "null"`;
- `source:aws_s3` says it is not available to ordinary Web Composer users;
- the CSV example has top-level `sources`, `on_success`, and `schema`; and
- every other source example includes a validation-failure policy.

- [ ] **Step 2: Run the family test and observe the incomplete inventory**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/sources/test_csv_source_metadata.py -q
```

Expected: failures for six empty sources and the invalid legacy CSV shape.

- [ ] **Step 3: Add source metadata**

Write concise prose from the table. Use secret-ref form for credential-bearing Azure configurations; normalize it only in the test helper. Do not change web authority, assistance, validation defaults, runtime behavior, or plugin versions in this package.

- [ ] **Step 4: Refresh all seven source hashes**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash

targets = {
    "src/elspeth/plugins/sources/aws_s3_source.py": "AWSS3Source",
    "src/elspeth/plugins/sources/azure_blob_source.py": "AzureBlobSource",
    "src/elspeth/plugins/sources/csv_source.py": "CSVSource",
    "src/elspeth/plugins/sources/dataverse.py": "DataverseSource",
    "src/elspeth/plugins/sources/json_source.py": "JSONSource",
    "src/elspeth/plugins/sources/null_source.py": "NullSource",
    "src/elspeth/plugins/sources/text_source.py": "TextSource",
}
for raw_path, class_name in targets.items():
    path = Path(raw_path)
    fix_source_file_hash(path, class_name, compute_source_file_hash(path))
PY
```

- [ ] **Step 5: Verify and commit the source package**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/sources/test_csv_source_metadata.py \
  tests/unit/web/catalog/test_policy_view.py -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
git diff --check
git add \
  src/elspeth/plugins/sources/aws_s3_source.py \
  src/elspeth/plugins/sources/azure_blob_source.py \
  src/elspeth/plugins/sources/csv_source.py \
  src/elspeth/plugins/sources/dataverse.py \
  src/elspeth/plugins/sources/json_source.py \
  src/elspeth/plugins/sources/null_source.py \
  src/elspeth/plugins/sources/text_source.py \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/sources/test_csv_source_metadata.py
git diff --cached --name-only
git commit -m "docs: complete source catalogue guidance"
```

---

### Task 2A: Correct Dataverse logical-name resolution exposed by the catalogue

**Why this is required:** Quality review of the source catalogue proved that
`DataverseSourceConfig.entity` is authored as a Dataverse `LogicalName`, while
the same value is currently reused as the Web API `EntitySetName`. A standard
Contact query therefore cannot make both the metadata probe and data request
correct. The catalogue must not publish an example that only validates locally.

**Files:**

- Modify: `src/elspeth/plugins/sources/dataverse.py`
- Modify: `src/elspeth/plugins/infrastructure/clients/dataverse.py`
- Modify: `tests/unit/plugins/sources/test_dataverse_source.py`
- Modify: `tests/integration/plugins/test_dataverse_pipeline.py`
- Modify: `tests/unit/plugins/sources/test_source_catalogue_metadata.py`

- [ ] **Step 1: Write seam-first failing tests**

Use `entity: contact` and a complete metadata response containing both
`LogicalName: contact` and `EntitySetName: contacts`. Add focused tests proving:

- structured mode requests
  `EntityDefinitions(LogicalName='contact')?$select=LogicalName,EntitySetName`
  and passes a `/contacts` URL, with the authored query options intact, to
  `paginate_odata`;
- FetchXML retains `<entity name="contact">` but resolves and passes `contacts`
  as the collection argument to `paginate_fetchxml`;
- malformed, missing, blank, non-string, or mismatched metadata identities fail
  closed before data pagination;
- a metadata 403 fails closed when no explicit fallback is configured; and
- a metadata 403 may continue only with an explicitly configured, validated
  `entity_set_name: contacts` fallback.

Run the new focused tests and confirm they fail because the runtime still reuses
the logical name as the collection path, not because of fixture or syntax errors.

- [ ] **Step 2: Implement the minimal resolver**

Add an optional `entity_set_name` field to `DataverseSourceConfig`. Treat
`entity` and FetchXML `<entity name>` exclusively as logical names. Replace the
existence-only probe with a resolver equivalent to:

```python
def _resolve_entity_set_name(self, ctx: SourceContext, logical_name: str) -> str:
    """Return the authoritative Web API collection name for a logical name."""
```

The resolver must select and validate `LogicalName` plus `EntitySetName`, record
the metadata call through the existing audit boundary, reject contradictory or
unusable response data, and never pluralize or silently reuse the logical name.
If metadata access returns 403, use the explicit fallback only; otherwise raise
an actionable `DataverseClientError`. A successful metadata response must agree
with any explicit fallback.

Change `_build_query_url` to require the resolved entity-set name explicitly,
and resolve both structured and FetchXML modes before constructing their data
requests. Rename/document the client pagination parameter as
`entity_set_name`; its transport behavior remains unchanged. Reorder error-path
URL bookkeeping so audit records never claim an unresolved collection URL.

- [ ] **Step 3: Make the catalogue example executable**

Change the Dataverse source example to `entity: contact` while retaining managed
identity, `select`, observed schema, and validation-failure policy. Extend the
family assertion so the singular logical-name contract cannot drift back to an
entity-set name.

- [ ] **Step 4: Refresh integrity metadata and verify**

Refresh `DataverseSource.source_file_hash`, then run:

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/sources/test_dataverse_source.py \
  tests/unit/plugins/infrastructure/clients/test_dataverse_client.py \
  tests/integration/plugins/test_dataverse_pipeline.py \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/sources/test_csv_source_metadata.py \
  tests/unit/web/catalog/test_policy_view.py -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes --root src/elspeth
.venv/bin/python scripts/wardline_gate.py
git diff --check
```

Expected: every focused test and both integrity gates pass. Commit the correction
separately from the catalogue metadata tranche so the runtime expansion remains
reviewable and attributable to `elspeth-b1efc0403b`.

---

### Task 3: Document all eight sinks

**Files:**

- Modify: `src/elspeth/plugins/sinks/aws_s3_sink.py`
- Modify: `src/elspeth/plugins/sinks/azure_blob_sink.py`
- Modify: `src/elspeth/plugins/sinks/chroma_sink.py`
- Modify: `src/elspeth/plugins/sinks/csv_sink.py`
- Modify: `src/elspeth/plugins/sinks/database_sink.py`
- Modify: `src/elspeth/plugins/sinks/dataverse.py`
- Modify: `src/elspeth/plugins/sinks/json_sink.py`
- Modify: `src/elspeth/plugins/sinks/text_sink.py`
- Create: `tests/unit/plugins/sinks/test_sink_catalogue_metadata.py`

| Plugin | Use when | Avoid when / boundary | Example basis | Tags |
|---|---|---|---|---|
| `sink:aws_s3` | One bounded cumulative CSV/JSON/JSONL object per run, default AWS chain, run-scoped conditional publication | Per-row objects, multipart/unbounded uploads, append/resume, inline credentials, or Composer custom endpoints | run-id key, CSV, `overwrite: false`, observed schema | `aws`, `s3`, `cloud`, `object-storage` |
| `sink:azure_blob` | One bounded cumulative cloud artifact with exactly one approved auth method | Append/resume, per-row blobs, mixed auth, or unbounded upload | Managed identity, run-id blob path, JSONL, `overwrite: false` | `azure`, `blob`, `cloud`, `object-storage` |
| `sink:chroma_sink` | Stable string ID/document plus scalar metadata for semantic retrieval/RAG | Authoritative archive, nested metadata, caller embeddings, resume, or duplicate policy other than recoverable overwrite | Persistent session-confined collection with explicit field mapping | `chroma`, `vector-store`, `embedding`, `rag` |
| `sink:csv` | Portable flat tabular output, controlled columns/headers, append/resume | Nested/binary data or columns that drift after first accepted row | observed schema, collision auto-increment | `csv`, `file`, `batch`, `tabular` |
| `sink:database` | Transactional append to operator-provisioned SQLite/PostgreSQL target and effect ledger | DDL, replace/drop, unsupported dialect, inline credentials, or missing ledger | provisioned SQLite example, append, explicit ledger permissions | `database`, `sql`, `tabular`, `exactly-once` |
| `sink:dataverse` | Idempotent upsert with explicit mapping and stable string alternate key | Create/update/delete/bulk modes, duplicate keys, arbitrary lookup URIs, or non-Dataverse endpoints | managed identity, contacts, mapping, alternate key | `dataverse`, `odata`, `crm`, `upsert` |
| `sink:json` | Structured JSON; JSONL for resumable append and array format for one complete artifact | JSON array for resume/long append, non-finite values, or flat text/table use cases | JSONL, collision auto-increment, observed schema | `json`, `jsonl`, `file`, `structured` |
| `sink:text` | Exactly one existing string field per canonical LF-delimited line | Multi-field/nested/multiline data or generic rejected-row preservation | fixed one-string-field schema | `text`, `file`, `line-oriented`, `single-field` |

- [ ] **Step 1: Write a failing live-registry sink test**

Assert the exact eight names, all four fields, config validation, no credential literals, and:

- Chroma uses `on_duplicate: overwrite`;
- database uses `if_exists: append` and supplies its effect ledger;
- S3 and Azure use `overwrite: false`; and
- local path examples remain relative/session-confineable.

- [ ] **Step 2: Run it red**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/sinks/test_sink_catalogue_metadata.py -q
```

Expected: eight incomplete-plugin failures.

- [ ] **Step 3: Add the eight metadata blocks**

Do not claim append/resume for cloud, Chroma, database, or Dataverse sinks where the implementation does not support it. Do not imply database DDL. Do not place `endpoint_url` in the Web Composer S3 example.

- [ ] **Step 4: Refresh the eight sink hashes**

Run the Task 2 hash script with this mapping:

```python
targets = {
    "src/elspeth/plugins/sinks/aws_s3_sink.py": "AWSS3Sink",
    "src/elspeth/plugins/sinks/azure_blob_sink.py": "AzureBlobSink",
    "src/elspeth/plugins/sinks/chroma_sink.py": "ChromaSink",
    "src/elspeth/plugins/sinks/csv_sink.py": "CSVSink",
    "src/elspeth/plugins/sinks/database_sink.py": "DatabaseSink",
    "src/elspeth/plugins/sinks/dataverse.py": "DataverseSink",
    "src/elspeth/plugins/sinks/json_sink.py": "JSONSink",
    "src/elspeth/plugins/sinks/text_sink.py": "TextSink",
}
```

- [ ] **Step 5: Verify and commit**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/sinks/test_sink_catalogue_metadata.py \
  tests/unit/web/catalog/test_policy_view.py -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
git diff --check
git add \
  src/elspeth/plugins/sinks/aws_s3_sink.py \
  src/elspeth/plugins/sinks/azure_blob_sink.py \
  src/elspeth/plugins/sinks/chroma_sink.py \
  src/elspeth/plugins/sinks/csv_sink.py \
  src/elspeth/plugins/sinks/database_sink.py \
  src/elspeth/plugins/sinks/dataverse.py \
  src/elspeth/plugins/sinks/json_sink.py \
  src/elspeth/plugins/sinks/text_sink.py \
  tests/unit/plugins/sinks/test_sink_catalogue_metadata.py
git diff --cached --name-only
git commit -m "docs: complete sink catalogue guidance"
```

---

### Task 4: Document the ten core and utility transforms

**Files:**

- Modify: `src/elspeth/plugins/transforms/blob_csv_expand.py`
- Modify: `src/elspeth/plugins/transforms/field_mapper.py`
- Modify: `src/elspeth/plugins/transforms/json_explode.py`
- Modify: `src/elspeth/plugins/transforms/keyword_filter.py`
- Modify: `src/elspeth/plugins/transforms/line_explode.py`
- Modify: `src/elspeth/plugins/transforms/passthrough.py`
- Modify: `src/elspeth/plugins/transforms/report_assemble.py`
- Modify: `src/elspeth/plugins/transforms/truncate.py`
- Modify: `src/elspeth/plugins/transforms/type_coerce.py`
- Modify: `src/elspeth/plugins/transforms/value_transform.py`
- Create: `tests/unit/plugins/transforms/test_core_catalogue_metadata.py`

| Plugin | Required selection distinction | Example authority | Tags |
|---|---|---|---|
| `blob_csv_expand` | Expand a payload-store CSV blob into rows; not a file source or arbitrary binary parser | `examples/blob_transforms/settings_expand_csv_blobs.yaml` | `csv`, `blob`, `tabular`, `fan-out` |
| `field_mapper` | Rename/copy/drop known fields; not expression evaluation or type coercion | `examples/deep_routing/settings.yaml` | `fields`, `mapping`, `rename`, `cleanup` |
| `json_explode` | Expand one JSON array field; not object flattening or batch aggregation | `examples/json_explode/settings.yaml` | `json`, `array`, `fan-out`, `deaggregation` |
| `keyword_filter` | Regex/pattern content screening with its documented routing semantics; not general expressions | `examples/error_routing/settings.yaml` | `filtering`, `regex`, `content-screening` |
| `line_explode` | Split a text field into rows; not source-file line reading or CSV parsing | owning unit-test config because no maintained example exists | `text`, `lines`, `fan-out`, `deaggregation` |
| `passthrough` | Explicit wiring/schema/debug boundary with unchanged row data; not business transformation | `examples/explicit_routing/settings.yaml` | `wiring`, `schema`, `debugging` |
| `report_assemble` | Batch-aware page/section report assembly under `aggregations`; not a per-row transform | `examples/report_assemble/settings.yaml` | `report`, `aggregation`, `batch`, `pagination` |
| `truncate` | Deterministically cap text length; not token-aware model context management | `examples/error_routing/settings.yaml` | `text`, `truncation`, `length-limit` |
| `type_coerce` | Explicit field type normalization; not arbitrary calculation | `examples/transform_pipeline/settings.yaml` | `types`, `coercion`, `normalization` |
| `value_transform` | Ordered expression-based field calculation with pass-through rows; not filtering/routing | `examples/transform_pipeline/settings.yaml` | `expressions`, `calculation`, `fields` |

- [ ] **Step 1: Write the failing family test**

Assert the exact ten names and validate every fragment. Require `report_assemble` under `aggregations`, all other examples under `transform`, and preserve the existing accurate value-transform Avoid warning.

- [ ] **Step 2: Run red, add content, and rerun green**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_core_catalogue_metadata.py -q
```

- [ ] **Step 3: Refresh hashes**

Use the Task 2 hash script with the ten file/class pairs:

```python
targets = {
    "src/elspeth/plugins/transforms/blob_csv_expand.py": "BlobCSVExpand",
    "src/elspeth/plugins/transforms/field_mapper.py": "FieldMapper",
    "src/elspeth/plugins/transforms/json_explode.py": "JSONExplode",
    "src/elspeth/plugins/transforms/keyword_filter.py": "KeywordFilter",
    "src/elspeth/plugins/transforms/line_explode.py": "LineExplode",
    "src/elspeth/plugins/transforms/passthrough.py": "PassThrough",
    "src/elspeth/plugins/transforms/report_assemble.py": "ReportAssemble",
    "src/elspeth/plugins/transforms/truncate.py": "Truncate",
    "src/elspeth/plugins/transforms/type_coerce.py": "TypeCoerce",
    "src/elspeth/plugins/transforms/value_transform.py": "ValueTransform",
}
```

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_core_catalogue_metadata.py -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
git diff --check
git add \
  src/elspeth/plugins/transforms/blob_csv_expand.py \
  src/elspeth/plugins/transforms/field_mapper.py \
  src/elspeth/plugins/transforms/json_explode.py \
  src/elspeth/plugins/transforms/keyword_filter.py \
  src/elspeth/plugins/transforms/line_explode.py \
  src/elspeth/plugins/transforms/passthrough.py \
  src/elspeth/plugins/transforms/report_assemble.py \
  src/elspeth/plugins/transforms/truncate.py \
  src/elspeth/plugins/transforms/type_coerce.py \
  src/elspeth/plugins/transforms/value_transform.py \
  tests/unit/plugins/transforms/test_core_catalogue_metadata.py
git diff --cached --name-only
git commit -m "docs: complete core transform catalogue guidance"
```

Before staging, inspect `git diff --name-only` and exclude every transform file not listed in this task.

---

### Task 5: Document the twelve batch and statistical transforms

**Files:**

- Modify the twelve `src/elspeth/plugins/transforms/batch_*.py` modules named below.
- Create: `tests/unit/plugins/transforms/test_batch_catalogue_metadata.py`

Every example belongs under `aggregations`, includes `output_mode: transform`, and uses either an explicit count trigger or deliberate end-of-source flush. Prose must say that `group_by` partitions one flushed batch and does not accumulate a group across windows.

| Plugin | Required selection distinction | Tags |
|---|---|---|
| `batch_classifier_metrics` | Actual/predicted scalar labels, confusion and F metrics; not score-to-label conversion; `None` pairs excluded | `batch`, `classification`, `narrative-summary` |
| `batch_data_quality_report` | One quality row per configured existing field; present `None` is missing, absent columns are errors | `batch`, `data-quality`, `profiling` |
| `batch_distribution_profile` | Numeric descriptive statistics, optional group profiles; not categorical frequency | `batch`, `distribution`, `narrative-summary` |
| `batch_drift_compare` | Same-window baseline/comparison cohorts; no history, p-value, alert threshold, or cross-run monitoring | `batch`, `drift`, `comparison` |
| `batch_effect_size` | Cohen's d/Hedges' g for unpaired numeric variants; not significance or paired analysis | `batch`, `effect-size`, `comparison` |
| `batch_experiment_compare` | Unpaired mean/lift/z/normal-bound comparison; no p-value | `batch`, `experiment`, `comparison` |
| `batch_outlier_annotator` | Window-local z/robust-z annotations; invalid numeric rows are reported but not emitted | `batch`, `outlier`, `annotation` |
| `batch_paired_preference` | Matched baseline/candidate rows by pair ID; split-window pairs do not join later | `batch`, `paired`, `comparison` |
| `batch_replicate` | Bounded per-row copy expansion; not sampling or unbounded fan-out | `batch`, `deaggregation`, `row-expansion` |
| `batch_stats` | Count/sum/optional mean over numeric field; original rows are replaced | `batch`, `aggregation`, `statistics` |
| `batch_threshold_summary` | Named threshold summary rows; not row filtering/routing/annotation | `batch`, `threshold`, `summary` |
| `batch_top_k` | Type-aware scalar frequencies; not numeric distribution profiling | `batch`, `frequency`, `top-k` |

- [ ] **Step 1: Write a failing family test**

Discover all registered transform names starting `batch_`; assert that the live set is exactly the twelve table entries. Validate every fragment through `AggregationSettings` as well as the shared plugin config check. Require `output_mode == "transform"` and preserve `narrative-summary` on the two narrative plugins.

- [ ] **Step 2: Run it red**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_batch_catalogue_metadata.py -q
```

- [ ] **Step 3: Add exact window-aware metadata**

Base each fragment on the matching file in `examples/statistical_batch_plugins/`, plus `examples/deaggregation/settings.yaml` for `batch_replicate` and `examples/batch_aggregation/settings.yaml` for `batch_stats`. Keep examples component-sized. Do not claim p-values, significance, cross-run state, or global grouping.

- [ ] **Step 4: Refresh all twelve hashes**

Use the Task 2 script with:

```python
targets = {
    "src/elspeth/plugins/transforms/batch_classifier_metrics.py": "BatchClassifierMetrics",
    "src/elspeth/plugins/transforms/batch_data_quality_report.py": "BatchDataQualityReport",
    "src/elspeth/plugins/transforms/batch_distribution_profile.py": "BatchDistributionProfile",
    "src/elspeth/plugins/transforms/batch_drift_compare.py": "BatchDriftCompare",
    "src/elspeth/plugins/transforms/batch_effect_size.py": "BatchEffectSize",
    "src/elspeth/plugins/transforms/batch_experiment_compare.py": "BatchExperimentCompare",
    "src/elspeth/plugins/transforms/batch_outlier_annotator.py": "BatchOutlierAnnotator",
    "src/elspeth/plugins/transforms/batch_paired_preference.py": "BatchPairedPreference",
    "src/elspeth/plugins/transforms/batch_replicate.py": "BatchReplicate",
    "src/elspeth/plugins/transforms/batch_stats.py": "BatchStats",
    "src/elspeth/plugins/transforms/batch_threshold_summary.py": "BatchThresholdSummary",
    "src/elspeth/plugins/transforms/batch_top_k.py": "BatchTopK",
}
```

- [ ] **Step 5: Verify and commit**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_batch_catalogue_metadata.py \
  tests/unit/plugins/transforms/test_batch_*.py -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
git diff --check
git add \
  src/elspeth/plugins/transforms/batch_*.py \
  tests/unit/plugins/transforms/test_batch_catalogue_metadata.py
git commit -m "docs: complete batch transform catalogue guidance"
```

---

### Task 6: Document the ten external and provider transforms

**Files:**

- Modify: `src/elspeth/plugins/transforms/web_scrape.py`
- Modify: `src/elspeth/plugins/transforms/blob_fetch.py`
- Modify: `src/elspeth/plugins/transforms/llm/transform.py`
- Modify: `src/elspeth/plugins/transforms/rag/transform.py`
- Modify: `src/elspeth/plugins/transforms/aws/bedrock_prompt_shield.py`
- Modify: `src/elspeth/plugins/transforms/aws/bedrock_content_safety.py`
- Modify: `src/elspeth/plugins/transforms/aws/textract_document_analysis.py`
- Modify: `src/elspeth/plugins/transforms/azure/content_safety.py`
- Modify: `src/elspeth/plugins/transforms/azure/prompt_shield.py`
- Modify: `src/elspeth/plugins/transforms/azure/document_intelligence.py`
- Create: `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`

| Plugin | Required selection and trust-boundary facts | Tags |
|---|---|---|
| `web_scrape` | Audited public HTTP(S) page extraction to Markdown/text; not authenticated APIs/binary documents; remote content is untrusted before LLM use | `http`, `network`, `scraping` |
| `blob_fetch` | Preserve original authorized HTTP(S) bytes plus MIME/size/hash metadata; not semantic extraction; no origin-auth option | `http`, `network`, `blob` |
| `llm` | Operator-approved profile, recorded prompts/responses/model/tokens; no provider credentials/endpoints in web-authored options | `llm`, `generation`, `structured-output` |
| `rag_retrieval` | Rank provenance-bearing context from an existing Chroma collection or Azure Search index; not indexing or answer generation; retrieved text remains untrusted | `rag`, `retrieval`, `vector-search` |
| `aws_bedrock_prompt_shield` | Pre-LLM prompt-attack blocking through an opaque operator profile and default AWS chain | `aws`, `bedrock`, `prompt-shield` |
| `aws_bedrock_content_safety` | Post-LLM harmful-content blocking; `source: OUTPUT` is required for output-control credit | `aws`, `bedrock`, `content-safety` |
| `aws_textract_document_analysis` | Async S3-backed OCR/forms/tables/etc.; S3 read scope required; do not reference the unregistered synchronous plugin | `aws`, `textract`, `document`, `ocr`, `enrichment` |
| `azure_content_safety` | Category thresholds for hate/violence/sexual/self-harm; threshold 6 is effectively non-blocking | `azure`, `content-safety`, `moderation` |
| `azure_prompt_shield` | Pre-LLM jailbreak/prompt-injection checks; distinguish user prompt, document, and both | `azure`, `prompt-shield`, `security` |
| `azure_document_intelligence` | URL/base64 document extraction; request audit retains URL or encoded body, so surface credential/data-retention implications | `azure`, `document`, `ocr`, `enrichment`, `http` |

- [ ] **Step 1: Write the failing external-family test**

Discover transforms whose `determinism` is `EXTERNAL_CALL` or `NON_DETERMINISTIC`; assert the exact ten-name set. Validate content, fragment shape, owning config, secret-ref placement, and credential literals. Add exact assertions that:

- `web_scrape` and `blob_fetch` include non-secret abuse contact/reason text;
- `llm` and both Bedrock transforms use operator profiles and no node-level credentials;
- Textract no longer recommends an unregistered plugin;
- Bedrock content safety sets `source: OUTPUT`;
- Azure Document Intelligence uses a secret-ref marker rather than legacy dollar-brace environment interpolation;
- RAG's Azure example uses its nested provider config; and
- every remote-content producer warns that returned content is untrusted before LLM consumption where applicable.

- [ ] **Step 2: Run red**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_external_catalogue_metadata.py -q
```

- [ ] **Step 3: Add or correct all ten metadata blocks**

Use dummy public documentation endpoints only where the config requires a URL. Use `{secret_ref: NAME}` for accepted credential fields and opaque profile names for operator-profiled plugins. Do not put a secret ref in abuse-contact or scraping-reason fields because those are wire-visible identity/audit text.

- [ ] **Step 4: Refresh all ten hashes**

Use the Task 2 script with:

```python
targets = {
    "src/elspeth/plugins/transforms/web_scrape.py": "WebScrapeTransform",
    "src/elspeth/plugins/transforms/blob_fetch.py": "BlobFetch",
    "src/elspeth/plugins/transforms/llm/transform.py": "LLMTransform",
    "src/elspeth/plugins/transforms/rag/transform.py": "RAGRetrievalTransform",
    "src/elspeth/plugins/transforms/aws/bedrock_prompt_shield.py": "AWSBedrockPromptShield",
    "src/elspeth/plugins/transforms/aws/bedrock_content_safety.py": "AWSBedrockContentSafety",
    "src/elspeth/plugins/transforms/aws/textract_document_analysis.py": "AWSTextractDocumentAnalysis",
    "src/elspeth/plugins/transforms/azure/content_safety.py": "AzureContentSafety",
    "src/elspeth/plugins/transforms/azure/prompt_shield.py": "AzurePromptShield",
    "src/elspeth/plugins/transforms/azure/document_intelligence.py": "AzureDocumentIntelligence",
}
```

- [ ] **Step 5: Verify and commit**

```bash
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/transforms/test_external_catalogue_metadata.py \
  tests/unit/plugins/transforms/test_web_scrape.py \
  tests/unit/plugins/transforms/test_blob_fetch.py \
  tests/unit/plugins/transforms/aws \
  tests/unit/plugins/transforms/azure \
  tests/unit/plugins/transforms/rag \
  tests/unit/plugins/llm -q
PYTHONPATH=elspeth-lints/src /home/john/elspeth/.venv/bin/python \
  -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
git diff --check
git add \
  src/elspeth/plugins/transforms/web_scrape.py \
  src/elspeth/plugins/transforms/blob_fetch.py \
  src/elspeth/plugins/transforms/llm/transform.py \
  src/elspeth/plugins/transforms/rag/transform.py \
  src/elspeth/plugins/transforms/aws/bedrock_prompt_shield.py \
  src/elspeth/plugins/transforms/aws/bedrock_content_safety.py \
  src/elspeth/plugins/transforms/aws/textract_document_analysis.py \
  src/elspeth/plugins/transforms/azure/content_safety.py \
  src/elspeth/plugins/transforms/azure/prompt_shield.py \
  src/elspeth/plugins/transforms/azure/document_intelligence.py \
  tests/unit/plugins/transforms/test_external_catalogue_metadata.py
git diff --cached --name-only
git commit -m "docs: complete provider transform catalogue guidance"
```

---

### Task 7: Remove the misleading frontend placeholder

**Files:**

- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx`
- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx`

- [ ] **Step 1: Replace the fallback expectation with failing presence tests**

Parameterize `null`, `""`, `"   "`, and `"\n\t"` across all three fields and assert that no Details button or details panel is rendered. Add a mixed case where one meaningful field renders Details but whitespace-only sibling sections do not. Retain the populated CSV disclosure, keyboard, Markdown, and accessibility tests.

- [ ] **Step 2: Run the focused test and observe the current Details/fallback behavior**

```bash
cd /home/john/elspeth/src/elspeth/web/frontend
npm test -- src/components/catalog/PluginCard.test.tsx
```

- [ ] **Step 3: Implement trim-aware presence**

Replace `PROSE_FALLBACK` and `allFallback` with:

```tsx
function hasCatalogText(value: string | null): value is string {
  return value !== null && value.trim().length > 0;
}

const hasWhen = hasCatalogText(plugin.usage_when_to_use);
const hasAvoid = hasCatalogText(plugin.usage_when_not_to_use);
const hasExample = hasCatalogText(plugin.example_use);
const hasDetails = hasWhen || hasAvoid || hasExample;
```

Render the Details button and panel only when `hasDetails`. Render each section only when its own predicate is true. Pass the original, untrimmed example to `<pre>` so YAML indentation is preserved.

- [ ] **Step 4: Verify and commit**

```bash
cd /home/john/elspeth/src/elspeth/web/frontend
npm test -- src/components/catalog/PluginCard.test.tsx
npm run typecheck
npm run lint
git diff --check
git add \
  src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx \
  src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx
git commit -m "fix: hide empty plugin catalogue details"
```

---

### Task 8: Add the registry-wide gate and cross-layer catalogue acceptance

**Files:**

- Create: `tests/unit/plugins/test_catalog_reference_content.py`
- Modify: `tests/unit/web/catalog/test_service.py`
- Modify: `tests/unit/web/catalog/test_routes.py`
- Modify: the existing Plugin Catalogue Playwright specification selected by:

`src/elspeth/web/frontend/tests/e2e/catalog-reshape.spec.ts`

- [ ] **Step 1: Write the final registry-wide gate**

Use `discover_builtin_references()` and require:

```python
assert len(references) == 47
assert Counter(item.kind for item in references) == {
    "source": 7,
    "transform": 32,
    "sink": 8,
}
```

Run all shared assertions for every discovered class. Add aggregate uniqueness checks for exact normalized Use, Avoid, and Example values across user-visible built-ins. Do not use fuzzy cross-plugin similarity thresholds; shared phrases are legitimate and fuzzy comparisons will create false positives.

Require the exact kind-qualified identities from Appendix A. This identity assertion belongs in a test because the program's acceptance target is the complete current catalogue; discovery still supplies the objects and a future addition will fail until reviewed.

- [ ] **Step 2: Prove API serialization parity**

In `test_service.py`, compare each summary's four fields with its declaring class for all 47 entries. In `test_routes.py`, add one representative source, ordinary transform, aggregation transform, and sink assertion so route serialization cannot drop a field that the service retains.

- [ ] **Step 3: Add browser acceptance**

Extend the existing catalogue E2E test to:

- open the drawer;
- open Details for `csv` source;
- open Details for `value_transform`;
- open Details for `database` sink;
- assert Use, Avoid, and Example labels and a plugin-specific phrase for each;
- close and reopen Details to prove collapsed-by-default behavior; and
- assert the old “See the technical description above.” text never appears.

Do not try to assert all 47 entries in Playwright; the registry/unit gate owns exhaustive coverage.

- [ ] **Step 4: Run focused cross-layer tests**

```bash
cd /home/john/elspeth
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest \
  tests/unit/plugins/test_catalog_reference_content.py \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/sinks/test_sink_catalogue_metadata.py \
  tests/unit/plugins/transforms/test_core_catalogue_metadata.py \
  tests/unit/plugins/transforms/test_batch_catalogue_metadata.py \
  tests/unit/plugins/transforms/test_external_catalogue_metadata.py \
  tests/unit/web/catalog/test_service.py \
  tests/unit/web/catalog/test_routes.py \
  tests/unit/web/catalog/test_policy_view.py -q

cd /home/john/elspeth/src/elspeth/web/frontend
npm test -- src/components/catalog/PluginCard.test.tsx
npm run typecheck
npx playwright test tests/e2e/catalog-reshape.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit the integration gate**

```bash
git add \
  tests/unit/plugins/test_catalog_reference_content.py \
  tests/unit/web/catalog/test_service.py \
  tests/unit/web/catalog/test_routes.py \
  src/elspeth/web/frontend/tests/e2e
git diff --cached --check
git commit -m "test: enforce complete plugin catalogue reference content"
```

---

### Task 9: Run full program verification and close out

- [ ] **Step 1: Confirm the live inventory and current-checkout import**

```bash
cd /home/john/elspeth
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/python - <<'PY'
from collections import Counter
from inspect import getsourcefile
from elspeth.plugins.infrastructure.manager import PluginManager

manager = PluginManager()
manager.register_builtin_plugins()
groups = {
    "source": manager.get_sources(),
    "transform": manager.get_transforms(),
    "sink": manager.get_sinks(),
}
print(Counter({kind: len(classes) for kind, classes in groups.items()}))
for kind, classes in groups.items():
    for cls in classes:
        path = getsourcefile(cls)
        assert path is not None and path.startswith("/home/john/elspeth/src/"), (kind, cls.name, path)
        assert cls.usage_when_to_use and cls.usage_when_to_use.strip()
        assert cls.usage_when_not_to_use and cls.usage_when_not_to_use.strip()
        assert cls.example_use and cls.example_use.strip()
        assert cls.capability_tags
PY
```

Expected counts: source 7, transform 32, sink 8.

- [ ] **Step 2: Run the Python CI-equivalent suite**

```bash
cd /home/john/elspeth
PYTHONPATH=/home/john/elspeth/src /home/john/elspeth/.venv/bin/pytest tests/
```

Expected: PASS. A scoped run is not sufficient for this repository.

- [ ] **Step 3: Run static and trust-tier gates**

```bash
cd /home/john/elspeth
PYTHONPATH=/home/john/elspeth/src:/home/john/elspeth/elspeth-lints/src \
  /home/john/elspeth/.venv/bin/python -m elspeth_lints.core.cli check
git diff --check
```

Expected: PASS. If trust-tier line movement invalidates judged metadata, stop and use the key-free judge staging workflow. Do not edit signatures.

- [ ] **Step 4: Run the complete frontend gates**

```bash
cd /home/john/elspeth/src/elspeth/web/frontend
npm run typecheck
npm run lint
npm run lint:css
npm test -- --run
npx playwright test
npm run build
```

Expected: PASS.

- [ ] **Step 5: Apply the Wardline decision**

Metadata-only class attributes and trim-aware display logic do not change an external-input boundary, so a Wardline scan is not newly required by this plan. If implementation changes YAML loading, secret resolution, URL/provider validation, or another boundary despite the scope rule, run:

```bash
cd /home/john/elspeth
.venv/bin/python scripts/wardline_gate.py
```

Fix any finding at the boundary and rescan.

- [ ] **Step 6: Review the final diff**

```bash
git status --short
CATALOGUE_BASE="$(git merge-base HEAD origin/main)"
git diff --stat "$CATALOGUE_BASE"...HEAD
git diff --name-only "$CATALOGUE_BASE"...HEAD
git log --oneline "$CATALOGUE_BASE"..HEAD
```

Verify:

- only the files named by this plan changed;
- all 47 identities in Appendix A are covered;
- no runtime config, policy, provider, execution, or secret-resolution code changed;
- every edited plugin hash was mechanically refreshed;
- the old placeholder string is absent; and
- unrelated worktree changes were neither staged nor overwritten.

- [ ] **Step 7: Close the Filigree program**

Attach the integration commit and exact verification commands to each content-package task. Close only tasks resolved by those commits. Reverify existing overlapping issues before closing them; do not close an issue merely because it shares a keyword.

---

## Filigree program topology

Create this graph only after selecting the post-release base:

- P2 milestone: **Complete published plugin catalogue guidance and prevent inventory drift**
  - P2 phase: contract and test kit — Task 1
  - P2 task: source reference content — Task 2
  - P2 task: sink reference content — Task 3
  - P2 task: core-transform reference content — Task 4
  - P2 task: batch-transform reference content — Task 5
  - P2 task: provider-transform reference content — Task 6
  - P2 task: frontend empty-details behavior — Task 7
  - P2 task: registry gate and integration closeout — Tasks 8–9

The five content tasks depend on the contract phase. Integration depends on all content tasks plus the frontend task. Claim work atomically with Filigree `work_start`; never claim with a separate status update.

Existing tracker overlap to requery before creating anything:

- `elspeth-1631e0b6ef` — residual configuration transform-table gap; adjacent, not a substitute for catalogue metadata;
- `elspeth-06566208b3` — internal null-source visibility; resolve consistently with the documented-but-hidden decision;
- `elspeth-5e0615bfab` — AWS S3 documentation, reportedly already fixed;
- `elspeth-ed5bf6f41d` — user-manual inventory, reportedly already fixed;
- `elspeth-a63806ce72` — field-mapper/value-transform example, reportedly already fixed;
- `elspeth-85970bca8b` — LLM reference, reportedly already fixed; and
- `elspeth-fc499e6d03` — row-union documentation; plugin-free and outside this program.

Do not put the broad catalogue milestone on the active 0.7.2 P0/P1 critical path.

## Out-of-scope findings to triage separately

The inventory audit found issues that must not be smuggled into documentation commits:

1. `examples/azure_blob_sentiment/settings.yaml` and its README use unsupported `pool_size` options on Azure safety transforms.
2. Prompt-shield advisory discovery omits some remote-content producers, including Azure Document Intelligence and RAG; `blob_fetch` needs an explicit chain-level policy decision.
3. `blob_fetch` wire-visible abuse-contact identity validation is not aligned with `web_scrape`.
4. RAG's nested Azure Search API-key requirement is not advertised by the flat catalogue secret-requirement mechanism.
5. Azure Blob and Dataverse sources need a separate owner decision on ordinary web authorability versus operator profiling.
6. Configuration and first-pipeline documentation contain singular `source:` and other source-option drift.
7. Catalogue search can match prose hidden inside collapsed Details; an excerpt or matched-detail affordance is a separate UX enhancement.

Reproduce each against the post-release base, search Filigree for duplicates, and then update/reuse/create the narrow issue. Documentation prose may truthfully describe current behavior; it must not “fix” these behaviors by promising future policy.

## Appendix A: authoritative acceptance inventory

The final registry test must account for these exact kind-qualified identities:

### Sources (7)

- `source:aws_s3`
- `source:azure_blob`
- `source:csv`
- `source:dataverse`
- `source:json`
- `source:null`
- `source:text`

### Transforms (32)

- `transform:aws_bedrock_content_safety`
- `transform:aws_bedrock_prompt_shield`
- `transform:aws_textract_document_analysis`
- `transform:azure_content_safety`
- `transform:azure_document_intelligence`
- `transform:azure_prompt_shield`
- `transform:batch_classifier_metrics`
- `transform:batch_data_quality_report`
- `transform:batch_distribution_profile`
- `transform:batch_drift_compare`
- `transform:batch_effect_size`
- `transform:batch_experiment_compare`
- `transform:batch_outlier_annotator`
- `transform:batch_paired_preference`
- `transform:batch_replicate`
- `transform:batch_stats`
- `transform:batch_threshold_summary`
- `transform:batch_top_k`
- `transform:blob_csv_expand`
- `transform:blob_fetch`
- `transform:field_mapper`
- `transform:json_explode`
- `transform:keyword_filter`
- `transform:line_explode`
- `transform:llm`
- `transform:passthrough`
- `transform:rag_retrieval`
- `transform:report_assemble`
- `transform:truncate`
- `transform:type_coerce`
- `transform:value_transform`
- `transform:web_scrape`

### Sinks (8)

- `sink:aws_s3`
- `sink:azure_blob`
- `sink:chroma_sink`
- `sink:csv`
- `sink:database`
- `sink:dataverse`
- `sink:json`
- `sink:text`

## Appendix B: package acceptance checklist

Each WP1–WP5 owner must hand off:

- the exact plugin identities owned;
- the plugin files changed;
- the family test's red and green commands;
- the hash-refresh command and plugin-hash gate result;
- confirmation that no runtime behavior, plugin version, authority, or policy changed;
- `git diff --check` output; and
- the commit ID.

The integration owner must independently rerun the registry gate, full Python suite, `elspeth-lints check`, full frontend suite, and final inventory script. Earlier package results are evidence, not a substitute for integration verification.
