# blob_json_expand — parse a JSON blob into pipeline rows

Date: 2026-08-26
Status: design approved, implementation not started

## Problem

A user asked the Web Composer for a pipeline that reads a remote JSON file,
splits each document into one row per section, gists each section with an LLM,
and regroups sections per document through a collector
(session `9412b3ee-ee23-405f-9548-fd570a07195e`). The planner refused across
three turns and built nothing.

The refusal was **correct**. Two facts establish it:

1. The web deployment authorizes exactly `REQUIRED_WEB_PLUGIN_IDS`
   (`src/elspeth/web/plugin_policy/compiler.py:17-38`), because
   `authorized = REQUIRED_WEB_PLUGIN_IDS | optional` (`compiler.py:106`),
   `optional` comes from `settings.plugin_allowlist` whose default is `()`
   (`src/elspeth/web/config.py:442`), and `deploy/elspeth-web.env` — the only
   `EnvironmentFile` the systemd unit loads — does not set
   `ELSPETH_WEB__PLUGIN_ALLOWLIST`. Authorized transforms are therefore
   `field_mapper`, `line_explode`, `llm`, `report_assemble`, `web_scrape`.
2. Even with the full installed registry, **no plugin parses a JSON blob into
   rows**. `blob_fetch` stores bytes and emits a `blob_ref`; `blob_csv_expand`
   parses CSV blobs; `json_explode` requires a value that is already a real
   list. `json_explode`'s own composer hint states the gap outright: "A
   JSON-looking STRING is not an array_field, and there is no transform that
   parses one into a list — the value must arrive list-shaped from the source."

Fetched JSON bytes are a dead end. This spec closes gap 2. Gap 1 is an operator
configuration decision recorded under "Out of scope" below.

## Target chain

```
text/csv source (one URL per row)
  └─ blob_fetch          url → blob_ref + blob_content_type   [EXTERNAL_CALL]
      └─ blob_json_expand  blob_ref → one row per record      [io_read]   ← NEW
          └─ json_explode    sections: list → one row per section
              └─ llm           one-sentence gist per section
                  └─ collector   scope_opener = json_explode, require_all
                      └─ json sink   one summary row per document
```

Only the third line is new. Everything else exists and composes today.

## Design

`blob_json_expand` is the structural twin of `blob_csv_expand`: same
`TransformDataConfig` base, same `blob_ref_field` input, same `io_read` audit
characteristic, same 1→N fan-out contract. Blob bytes are Tier-3 untrusted, so
parse failures are value-level errors routed by `on_error`, never crashes.

### Configuration

```yaml
transform:
  plugin: blob_json_expand
  options:
    blob_ref_field: blob_ref
    content_type_field: blob_content_type
    data_key: documents
    format: json                  # optional; inferred from content type
    fields: [document_id, title, sections]
    field_mapping: {}
    include_record_index: false
    record_index_field: json_record_index
    max_output_rows: 100000
    max_blob_bytes: 104857600
    schema: {mode: observed}
```

### Decisions

**Nested values pass through as real Python values.** A record's `sections`
list arrives in the row as an actual list under a field typed `any`. This is
the only projection under which the target chain composes: `json_explode`
requires a list-shaped value and explicitly refuses a JSON-looking string.
Scalars stay scalars. Consequence accepted: a row field may hold an arbitrarily
deep structure that the schema DSL cannot describe beyond `any`. Depth and
value-size bounds were offered and deliberately declined; `max_output_rows` and
`max_blob_bytes` remain, matching `blob_csv_expand:49-50`.

**Record selection uses `data_key`, verbatim as the `json` source defines it.**
One optional top-level object key naming the record array; omitted means the
document's top level must itself be an array. A nested path grammar
(`payload.items`, JSON Pointer) was considered and rejected: it would introduce
a second selector vocabulary for the same job across two plugins. If a real
payload later demands nested selection, that is a separate ticket driven by
that payload, not a guess made now.

**`fields` is required, and every declared field is typed `any`.** JSON records
have no header row, so nothing else can tell downstream DAG validation that
`sections` will exist — without it `json_explode(array_field: sections)` cannot
validate. Each declared field becomes a required output field. A record missing
a declared key is a value-level error, so heterogeneous records are caught at
the boundary rather than surfacing as a missing field several nodes later.
`any` is the honest type: the real type is unknown until untrusted bytes are
parsed. Downstream nodes needing a concrete type use `type_coerce`.

**Format is inferred from the stored content type, fail-closed.**
`application/json` → json; `application/jsonl`, `application/x-ndjson`,
`text/jsonl` → jsonl. Anything else — including `text/plain`, and including the
content-type field being absent from the row — is a config-time error naming
`format` as the remedy. Nothing guesses. `blob_fetch` always writes its
`content_type_field` (declared `required=True` in its output schema,
`blob_fetch.py:210`), so the field is present whenever `blob_fetch` is upstream.

**Shared defaults are derived, not restated.** `blob_fetch.blob_ref_field` and
`blob_fetch.content_type_field` defaults are lifted into one shared constant
that all three blob transforms import. `blob_csv_expand` currently repeats the
`"blob_ref"` literal independently; that is the same defect one plugin earlier
and is fixed in the same commit. A default duplicated in three places drifts the
day one of them changes.

### Record → row projection

Deep-copy the upstream row, then overlay the record's keys — so the originating
URL or document id stays on every emitted row for disambiguation, matching
`blob_csv_expand:345`.

- **Key normalization** — record keys normalized to lowercase identifiers
  (`TicketID` → `ticketid`), `field_mapping` to override. Identical to the
  `json` source and `blob_csv_expand`.
- **Collision** — a record key colliding with an existing input field is a
  value-level `field_collision` error naming the colliding fields, per
  `blob_csv_expand:449-459`.
- **Non-object record** — an array element that is not a JSON object is a
  value-level error.
- **Empty array** — zero emitted rows, audited, not an error.
- **Bounds** — `max_output_rows` counted per input blob; `max_blob_bytes`
  checked before decode.

### Error semantics

Config-time (`PluginConfigError`, pipeline never builds): empty, duplicate, or
non-identifier entries in `fields`; `record_index_field` colliding with
`blob_ref_field` or a declared field; unknown `format`; `format` omitted where
the content type is absent or unrecognised.

Value-level (`TransformResult.error`, routed by `on_error`): blob missing or
integrity-failed; blob over `max_blob_bytes`; decode failure; JSON or JSONL
parse failure; `data_key` absent or not an array; top level not an array when
`data_key` is omitted; record not an object; record missing a declared field;
field collision; `max_output_rows` exceeded.

### Trust boundary

The plugin parses bytes fetched over HTTP from an operator-bounded but
externally-controlled origin — a Tier-3 parsing boundary requiring
`@trust_boundary` metadata from `src/elspeth/contracts/trust_boundary.py`,
which the Wardline pack also consumes. `blob_csv_expand` has the identical
trust shape; its marking is the template and is copied rather than reinvented.

## Implementation checklist

Derived empirically from the two commits that landed the sibling plugins:
`f1eefbf6b` ("feat: add blob fetch expansion transforms", 25 files) and its
follow-up `2a86267af` ("fix: stabilize blob fetch release checks", 10 files).
The follow-up is the important one — it is the set of whole-tree gates that
went red *after* a locally-green landing.

### Wave 1 — the plugin

1. `src/elspeth/plugins/transforms/blob_json_expand.py` — CREATE.
2. `src/elspeth/plugins/transforms/blob_fetch.py`,
   `blob_csv_expand.py` — UPDATE: lift shared field-name defaults to one
   constant both import.
3. `src/elspeth/contracts/errors.py` — UPDATE if new
   `TransformErrorReason` variants are needed (the sibling commit added 23
   lines here).
4. `tests/unit/plugins/transforms/test_blob_json_expand.py` — CREATE, written
   first. Mirrors `test_blob_csv_expand.py`, plus the JSON-only cases: nested
   list survives as a real list; JSONL; content-type inference including every
   fail-closed case; heterogeneous record rejection; `data_key` absent.
5. `tests/unit/plugins/test_discovery.py`,
   `tests/unit/plugins/test_validation_path_agreement.py` — UPDATE.

### Wave 2 — web/composer surfaces

6. `src/elspeth/web/audit_readiness/boundary_expectations.py` — UPDATE:
   add the determinism entry (`blob_csv_expand` is `io_read`; see the
   `blob_fetch` entry at line 203 as template).
7. `src/elspeth/web/audit_readiness/service.py` — UPDATE.
8. `src/elspeth/web/composer/guided/chat_solver.py` — UPDATE.
9. `src/elspeth/web/execution/validation.py` — UPDATE.
10. `tests/unit/web/audit_readiness/test_boundary_predicate_parity.py`,
    `tests/unit/web/execution/test_validation.py` — UPDATE.

### Wave 3 — whole-tree gates (the ones that bit last time)

11. `tests/golden/web/catalog/knob_schema/transform__blob_json_expand.json` —
    CREATE. No `--update` flag exists:
    `tests/unit/web/catalog/test_knob_schema_golden.py` byte-compares
    `json.dumps(payload, indent=2, sort_keys=True) + "\n"` **and** asserts the
    snapshot file set equals the live catalog's plugin set, so a missing file
    fails twice. Generate by running `CatalogServiceImpl(get_shared_plugin_manager())`
    and writing `_stable_json` for the new key.
12. `tests/golden/state_engine/plugin_lifecycle_matrix.json` — UPDATE.
13. `config/cicd/enforce_guard_symmetry/landscape.yaml`,
    `config/cicd/enforce_gve_attribution/structural.yaml` — UPDATE.
14. `config/cicd/contracts-whitelist.yaml` — UPDATE. Per house rule, any
    whitelist addition pairs a cleanup ticket and a removal trigger.
15. `tests/unit/telemetry/test_plugin_wiring.py`,
    `tests/unit/test_no_hasattr_branching.py` — UPDATE.
16. `src/elspeth/tui/screens/explain_screen.py` — UPDATE if the sibling's
    4-line change has an analogue.

### Wave 4 — examples and docs

17. `examples/blob_transforms/settings_expand_json_blobs.yaml` — CREATE,
    exercising the full chain end to end. The chain composing is the actual
    deliverable; unit tests will not prove it.
18. `examples/blob_transforms/README.md`, `examples/README.md`,
    `examples/AGENTS.md` — UPDATE.
19. `docs/agents/recent-code-hints.md` — UPDATE in the same commit if any new
    convention or whole-tree trap is discovered.

### Verification

- Targeted: `pytest tests/unit/plugins/transforms/test_blob_json_expand.py -n 0`
- Catalog gate: `pytest tests/unit/web/catalog/test_knob_schema_golden.py -n 0`
- Full suite as a background job in a dedicated worktree, never inline and never
  in the shared checkout.
- `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
- `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`,
  compared against the pre-change finding corpus rather than against zero.

The fixture `website/tutorial-site/multi-doc-sections.json` is used read-only as
a corpus witness. It is never reshaped to suit the plugin: if the plugin cannot
read it, the plugin is wrong.

## Out of scope

**Authorizing the chain on the web surface.** `blob_fetch` and
`blob_json_expand` remain invisible to the Web Composer until
`ELSPETH_WEB__PLUGIN_ALLOWLIST` in `deploy/elspeth-web.env` names them. That is
an operator decision on an outward-facing gov deployment, and `blob_fetch`
opens an outbound HTTP egress path there — bounded by SSRF validation, an exact
MIME allowlist, and mandatory wire-visible `abuse_contact` / `fetch_reason`
headers, but new nonetheless. Pending the operator's call.

**No changes** to `json_explode`, `blob_fetch` behaviour, or the collector. The
only pre-existing behaviour touched is the shared-default extraction in item 2.
