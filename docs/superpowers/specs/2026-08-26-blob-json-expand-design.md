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
      └─ blob_json_expand  blob_ref → one row per record      [IO_READ]   ← NEW
          └─ json_explode    sections: list → one row per section
              └─ llm           one-sentence gist per section
                  └─ collector   scope_opener = json_explode, require_all
                      └─ json sink   one summary row per document
```

Only the third line is new. Everything else exists and composes today.

## Design

`blob_json_expand` is the structural twin of `blob_csv_expand`: same
`TransformDataConfig` base, same `blob_ref_field` input, same
`determinism = Determinism.IO_READ`, same 1→N fan-out contract. Blob bytes are
untrusted, so parse failures are value-level errors routed by `on_error`, never
crashes.

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
    include_record_index: true    # MUST default true — see below
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
the boundary rather than surfacing several nodes later. `any` is the honest
type: the real type is unknown until untrusted bytes are parsed. Downstream
nodes needing a concrete type use `type_coerce`.

**Format is inferred from the stored content type, fail-closed.**
`application/json` → json; `application/jsonl`, `application/x-ndjson`,
`text/jsonl` → jsonl. Anything else — including `text/plain`, and including the
content-type field being absent from the row — is a config-time error naming
`format` as the remedy. Nothing guesses. `blob_fetch` always writes its
`content_type_field` (declared `required=True`, `blob_fetch.py:210`).

**`include_record_index` must default to `true`.** This is not a taste call: an
invariant requires `self_created_input_fields` to be non-empty under the probe
config (`tests/invariants/test_input_options_do_not_name_created_fields.py:193`).
`blob_csv_expand` satisfies it only because `include_row_index` defaults `True`
(`blob_csv_expand.py:47`). Defaulting the index off makes the plugin
unprobeable and reddens the invariant sweep.

**Shared defaults are derived, not restated.** `blob_fetch.blob_ref_field` and
`blob_fetch.content_type_field` defaults are lifted into one shared constant
that all three blob transforms import. `blob_csv_expand` currently repeats the
`"blob_ref"` literal independently; that is the same defect one plugin earlier
and is fixed in the same commit.

### Record → row projection

Deep-copy the upstream row, then overlay the record's keys — so the originating
URL or document id stays on every emitted row, matching `blob_csv_expand:345`.

- **Key normalization** — record keys normalized to lowercase identifiers
  (`TicketID` → `ticketid`), `field_mapping` to override.
- **Collision** — a record key colliding with an existing input field is a
  value-level `field_collision` error, per `blob_csv_expand:449-459`.
- **Non-object record** — an array element that is not a JSON object is a
  value-level error.
- **Empty array** — zero emitted rows, audited, not an error.
- **Bounds** — `max_output_rows` per input blob; `max_blob_bytes` before decode.

### Error semantics

Config-time (`PluginConfigError`): empty, duplicate, or non-identifier entries
in `fields`; `record_index_field` colliding with `blob_ref_field` or a declared
field; unknown `format`; `format` omitted where the content type is absent or
unrecognised. Rejection messages **must contain the literal option name**
(`test_input_options_do_not_name_created_fields.py:220`).

Value-level (`TransformResult.error`, routed by `on_error`): blob missing or
integrity-failed; blob over `max_blob_bytes`; decode failure; JSON or JSONL
parse failure; `data_key` absent or not an array; top level not an array when
`data_key` is omitted; record not an object; record missing a declared field;
field collision; `max_output_rows` exceeded.

### Trust boundary — no decorator

**Correction to an earlier draft of this spec:** `blob_csv_expand` marks its
parse boundary with **no `@trust_boundary` decorator at all**, and neither does
`pdf_rasterize`. Tree-wide the decorator is used by engine/web/contracts modules
and two AWS Textract helpers — never by the blob-expand family. Do not add one.

The convention to copy is narrow, typed `except` clauses that quarantine the row
via `TransformResult.error` with a declared `TransformErrorCategory` —
`blob_csv_expand.py:297` (`PayloadNotFoundError`), `:307` (`IntegrityError`),
`:325` (`UnicodeDecodeError`), `:340`, `:401`, `:425`, `:443`, `:445`. No
blanket `except Exception`.

**Design to avoid a tier_model allowlist entry, not to earn one.**
`blob_csv_expand` and `pdf_rasterize` each have zero entries in
`config/cicd/enforce_tier_model/`. `blob_fetch` has four, all R1 `.get()` reads
of a remote HTTP `Content-Type` — genuine network-response parsing, which a
payload-store reader does not do. If the JSON parser reaches for
`payload.get(key, default)` over decoded untrusted JSON, or uses a bare
`except Exception`, it trips R1/R6 and drags the work across the operator-signed
judge seam ([O1]). Per AGENTS.md: remove a lint finding, don't seal it.

### Determinism, not audit_characteristics

**Correction to an earlier draft:** declare `determinism = Determinism.IO_READ`
and **no `audit_characteristics`**. `blob_csv_expand` declares none; the
`io_read` audit flag is derived from determinism by the catalog service.
`audit_characteristics` exists only for what the framework cannot derive
(`CREDENTIALS`, `QUARANTINE`, `COERCE`, `SIGNED`, `PROVENANCE`); it defaults to
`frozenset()` at `base.py:327`.

`determinism` must be in the class `__dict__` — `BaseTransform.__init_subclass__`
(`base.py:612`) raises `TypeError` at registration if inherited. `IO_READ` is
also what keeps the plugin in the **core** catalogue rather than the external
one: `test_external_catalogue_metadata.py:53-56` derives membership from
`determinism in {EXTERNAL_CALL, NON_DETERMINISTIC}`.

## Implementation checklist

Derived empirically from both witnesses. The authoritative one is
`da5838874` ("whole-tree pins for pdf_rasterize"), which landed **four days
after** that plugin's own 5-file introducing commit `922967abf` and covers six
surfaces no grep for the plugin name would find. `blob_csv_expand`'s
`f1eefbf6b` (25 files) plus its follow-up `2a86267af` tell the same story: the
introducing commit is green locally and the whole-tree gates go red afterwards.

### Blocker — a test currently asserts this plugin does not exist

`tests/unit/plugins/transforms/test_external_catalogue_metadata.py:523-524`:

```python
    assert "blob_json_expand" not in registered
    assert "blob_json_expand" not in hints
```

This is the only pre-existing mention of the name in the tree, and it reddens
the moment the file lands. **Do not flip `not in` → `in`** — a guard must derive
from the authority it enforces. Rewrite it as: every parser named in
`blob_fetch`'s composer hints is in `registered`. That also discharges the
paired obligation at `blob_fetch.py:345`, whose hint currently says "chain the
registered blob_csv_expand transform for CSV content or another registered
parser after it" — until it names the JSON parser, `blob_fetch` keeps steering
planners past the plugin we just built.

### Phase 1 — the plugin

1. `src/elspeth/plugins/transforms/blob_json_expand.py` — CREATE. Registration
   is directory-walk (`discovery.py:289`); there is no registry import to edit.
   Class body must declare `name`, `determinism = Determinism.IO_READ`,
   `plugin_version = "1.0.0"` (not `"0.0.0"`), `source_file_hash` (may not be
   `None` despite the `str | None` annotation), `config_model`, all four of
   `usage_when_to_use` / `usage_when_not_to_use` / `example_use` /
   `capability_tags` in the class body rather than inherited,
   `output_naming_config_keys` naming every option that writes a column,
   `creates_tokens = True`, `passes_through_input = True`, and a concrete
   `probe_config()`. `capability_tags` must be a real `tuple`, 2-6 unique
   entries matching `^[a-z0-9]+(?:-[a-z0-9]+)*$`.
   `__init__` must call `self._initialize_declared_input_fields(cfg)`.
2. `source_file_hash` bootstrap — one-shot; see regeneration commands.
3. `src/elspeth/plugins/transforms/blob_fetch.py`, `blob_csv_expand.py` —
   UPDATE: shared field-name defaults, and the `blob_fetch` hint above.
   ⚠️ Editing `blob_fetch.py` moves its **own** `source_file_hash` — re-run the
   bootstrap on it too.
4. `src/elspeth/contracts/errors.py` — UPDATE. Reuse the existing
   `"expanded_blob"` action category and the shared blob-level error categories
   (`blob_not_found`, `blob_too_large`, `decode_failed`, `too_many_rows`); add
   only JSON-specific literals and `NotRequired` reason keys.
5. `tests/unit/plugins/transforms/test_blob_json_expand.py` — CREATE, written
   first. Mirrors the CSV sibling plus the JSON-only cases: nested list survives
   as a real list; JSONL; every fail-closed content-type inference case;
   heterogeneous record rejection; `data_key` absent.
   ⚠️ Hermetic probe payload record keys must exceed 12 characters — the
   conftest Hypothesis field-name generator caps at `max_size=12` and short keys
   collide into bogus "dropped field" failures.

### Phase 2 — explicit registries

6. `src/elspeth/web/audit_readiness/boundary_expectations.py` — UPDATE: add
   `"blob_json_expand": Determinism.IO_READ,` beside `:202`. One edit covers
   three assertions; `EXPECTED_BOUNDARY_TRANSFORMS` (`:236`) is derived. **Do
   not bump `_BOUNDARY_RULE_VERSION`** in `service.py` — adding a plugin to an
   existing class is not a rule change.
7. `config/cicd/contracts-whitelist.yaml` — UPDATE, exactly two
   `allowed_dict_patterns` entries (`probe_config:return`, `__init__:options`),
   format `file_path:context:param_name`. File a paired Filigree cleanup ticket
   with a removal trigger, as `922967abf` did.
8. `tests/unit/plugins/test_validation_path_agreement.py` — UPDATE: one
   rejection case. The completeness gate is conditional on the config having a
   `@model_validator`; ours will, so expect red without this.
9. `tests/unit/plugins/transforms/test_external_catalogue_metadata.py` — the
   blocker above.
10. `tests/unit/plugins/transforms/test_core_catalogue_metadata.py` — UPDATE:
    `EXPECTED_CORE_TAGS` (order-sensitive tuple) and `_REQUIRED_GUIDANCE`
    (absence is a `KeyError`). Note this file stays green with zero coverage if
    skipped — its roster assertion is a tautology for additions.

### Phase 3 — exact-count pins

Live counts are 9 sources / 34 transforms / 9 sinks = 52. Fifteen sites move
52→53, 34→35, 73→74, 42→43, 48→49 across `scripts/state_engine_plugin_matrix.py`,
`tests/unit/plugins/test_state_engine_plugin_matrix.py`,
`tests/unit/plugins/test_discovery.py`,
`tests/unit/plugins/test_catalog_reference_content.py`, and
`tests/unit/web/catalog/test_service.py:60`. That last file is easy to miss —
no grep for the plugin name finds it.

### Phase 4 — goldens and proof catalogs

11. `tests/golden/web/catalog/knob_schema/transform__blob_json_expand.json` —
    CREATE. Exact set equality: a missing golden fails, a stale one fails. No
    update flag exists.
12. `tests/golden/state_engine/plugin_lifecycle_matrix.json` — CREATE entry via
    `render-skeleton`, then hand-adjudicate the five reviewed fields it leaves
    `UNCLASSIFIED`. Mirror `blob_csv_expand`: `variants ["default"]`,
    `external_observation_required false`, `applicable_pb_boundaries
    ["PB-02","PB-09"]`, `local_fixture "hermetic"`, `release_lane "local"`.
13. `tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py` —
    UPDATE two sites: the row fixture and the harness blob fill. Without them
    the matrix silently runs the plugin against a default row with no blob.
    `example_use` must be constructible YAML — the harness builds every subject
    from it.
14. `docs/architecture/state_engine/proof-catalog/v3/catalog.json` — CREATE
    PB-09 case #74, hand-authored, mirroring `transform:blob_csv_expand`.
15. `docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json` —
    CREATE **40 cells** (10 PB-09 dimensions × 4 lanes), not 5 node ids.
16. `docs/architecture/state_engine/proof-catalog/v2/catalog.json` — UPDATE the
    mirrors, then rotate three digests: `V2_CATALOG_SHA256`
    (`test_state_engine_catalog_contract.py:35`) and both
    `CANONICAL_V2_{LEGS,EXECUTION_PROFILES}_SHA256`
    (`state_engine_assessment_lib/common.py:117,122`).
    Items 15 and 16 are the two `da5838874`'s own research doc missed and only
    the validators caught.
17. `tests/unit/core/dag/canonical_hash_corpus.json` — CONDITIONAL, triggered
    only by shipping a new `examples/*/settings*.yaml`. The diff must show
    exactly one new key and zero moved hashes.

### Phase 5 — examples and docs (not gated)

18. `examples/blob_transforms/settings_expand_json_blobs.yaml` — CREATE. The
    chain composing end to end is the actual deliverable; unit tests will not
    prove it. Adding it pulls in item 17. `examples/README.md` and
    `examples/AGENTS.md` need no edit — only new example *directories* are gated.
19. `docs/reference/configuration.md:917` — UPDATE the transform table row.
20. Same-commit prose drift: three comments still say the derived-declaration
    set has six members (`core/dag/schema_validation.py:1442`,
    `core/dag/models.py:227`, `web/composer/state.py:4132`). `pdf_rasterize`
    made it seven; this makes it eight.

### Explicitly NOT needed

Verified absent, recorded so nobody goes looking: `web/execution/validation.py`
(its `_WEB_FETCH_TRANSFORMS` gates plugins that open outbound HTTP themselves —
adding ours would be a false positive); `web/composer/guided/chat_solver.py` and
`guided/skills/` (no per-plugin entry); `contracts/plugin_semantics.py`,
`core/dag/schema_validation.py`, `core/dag/models.py`, `web/composer/state.py`
(prose and examples only — the mechanisms run off declarations);
`web/plugin_policy/*` (compiled from the live registry); all frontend/TypeScript
(a plugin name is not a new `node_type`, so the WS6 collector parity sweep is
not triggered); the acronym tables (`json` is already in both);
`tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json` (enumerates
findings, not files; regeneration needs the HMAC key — [O1], no agent may run
it); `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` (per-file hashing,
no blob entries, registry digests hash the manifest not the roster).

### Verification

- Targeted: `pytest tests/unit/plugins/transforms/test_blob_json_expand.py -n 0`
- Invariants: `pytest tests/invariants/ -n 4` — baseline today is
  `1 failed, 101 passed, 15 skipped`; the red is a pre-existing
  `llm.output_fields` issue, unrelated. A missing `probe_config` override shows
  up as a **silent skip**, not a failure, so compare skip counts too.
- Contracts: `cd /home/john/elspeth && .venv/bin/python scripts/check_contracts.py`
  — the `cd` is load-bearing; the wrong CWD prints a false all-green.
- Full suite as a background job in a dedicated worktree, never inline in the
  shared checkout.
- `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
- `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`,
  compared against the pre-change finding corpus rather than against zero.

The fixture `website/tutorial-site/multi-doc-sections.json` is used read-only as
a corpus witness. It is never reshaped to suit the plugin: if the plugin cannot
read it, the plugin is wrong.

### Regeneration commands, in order

```bash
source /home/john/elspeth/.venv/bin/activate
cd /home/john/elspeth                      # load-bearing

# 1. source_file_hash bootstrap — BOTH new plugin AND blob_fetch
python -c "
from pathlib import Path
from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash
for f, cls in (('blob_json_expand.py','BlobJSONExpand'), ('blob_fetch.py','BlobFetch')):
    p = Path('src/elspeth/plugins/transforms/') / f
    fix_source_file_hash(p, cls, compute_source_file_hash(p)); print(f, compute_source_file_hash(p))"

# 2. knob-schema golden (no update flag exists)
python - <<'EOF'
import json
from pathlib import Path
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
svc = CatalogServiceImpl(get_shared_plugin_manager())
d = Path("tests/golden/web/catalog/knob_schema")
for (kind, name) in sorted(svc._schema_cache):
    info = svc._schema_cache[(kind, name)]
    (d / f"{kind}__{name}.json").write_text(
        json.dumps({"plugin_kind": kind, "plugin_name": name,
                    "knob_schema": info.knob_schema}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
EOF

# 3. lifecycle matrix — exits 1 with UNCLASSIFIED until the 5 fields are filled
python scripts/state_engine_plugin_matrix.py render-skeleton tests/golden/state_engine/plugin_lifecycle_matrix.json
python scripts/state_engine_plugin_matrix.py check tests/golden/state_engine/plugin_lifecycle_matrix.json

# 4. proof catalogs and selectors — hand-authored, then validated
python scripts/state_engine_assessment.py validate-catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json
python scripts/state_engine_assessment.py validate-catalog docs/architecture/state_engine/proof-catalog/v2/catalog.json
python scripts/state_engine_assessment.py validate-selectors \
    docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json \
    --catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json

# 5. canonical hash corpus — ONLY if a new examples/*/settings*.yaml landed
ELSPETH_CANONICAL_CORPUS_RECORD=1 python -m pytest tests/unit/core/dag/test_canonical_hash_corpus.py -x
python -m pytest tests/unit/core/dag/test_canonical_hash_corpus.py -x
```

## Out of scope

**Authorizing the chain on the web surface.** `blob_fetch` and
`blob_json_expand` remain invisible to the Web Composer until
`ELSPETH_WEB__PLUGIN_ALLOWLIST` in `deploy/elspeth-web.env` names them. That is
an operator decision on an outward-facing gov deployment, and `blob_fetch` opens
an outbound HTTP egress path there — bounded by SSRF validation, an exact MIME
allowlist, and mandatory wire-visible `abuse_contact` / `fetch_reason` headers,
but new nonetheless. Pending the operator's call.

**No changes** to `json_explode` or the collector. The only pre-existing
behaviour touched is the shared-default extraction and the `blob_fetch` hint.

## Unrelated finding, worth a separate ticket

`plugin_hashes.PLUGIN_DIRS` omits `plugins/transforms/aws`, which
`PLUGIN_SCAN_CONFIG` does scan — AWS transforms are discovered but never
hash-gated.
