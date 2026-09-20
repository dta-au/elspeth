# Hash-column CHECK census (2026-09-21)

Follow-up to elspeth-f99b16fc2f (`blob_inline_resolutions.content_hash` CHECK
enforced length only). The ticket was one instance; this note records what a
census of every hash-shaped column found, and what it did not examine.

## How it was measured

The census reads the live SQLAlchemy metadata for both stores
(`elspeth.web.sessions.models.metadata`, `elspeth.core.landscape.schema.metadata`),
asks each `CheckConstraint` whether it is created for the SQLite and the
PostgreSQL compiler (so `ddl_if` is honoured), compiles its text for that
dialect, and classifies each column whose name contains `hash`, `sha256`,
`digest`, `fingerprint`, `checksum` or ends `_hex`:

- **ok** — on both dialects a CHECK anchors the length *and* the lowercase-hex
  alphabet. PostgreSQL may anchor length in the regex quantifier
  (`~ '^[0-9a-f]{64}$'`).
- **PARTIAL** — some CHECK mentions the column but one anchor is missing.
- **none** — no CHECK constrains the column's shape.

Controls: `blobs.content_hash` (known good) reads ok; the ticket's column read
PARTIAL before the fix and ok after, and the before/after diff is exactly the
five fixed columns. The first version of the instrument mis-scored 15 Landscape
columns as PARTIAL because it did not recognise the `{64}` quantifier or the
`::text` casts PostgreSQL-form checks carry; that was corrected before any
number here was taken. Column selection is by name, so a hash stored under an
unrelated name is outside this census.

| | ok | PARTIAL | none |
|---|---|---|---|
| before | 42 | 5 | 61 |
| after | 47 | 0 | 61 |

## Fixed on this branch (session epoch 63)

All five PARTIAL columns were length-only, `NOT NULL`, and written by a single
code path that computes the digest, so none was reachable as a live bug. Each
now uses `_lower_sha256_constraints`:

- `blob_inline_resolutions.content_hash` — the ticket.
- `blob_replacement_cleanups.old_blob_snapshot_hash`,
  `.replacement_blob_snapshot_hash`, `.old_content_hash`,
  `.replacement_content_hash`. Commit `fec6a4f32` added these length-only in
  the same change that gave the sibling `blob_deletion_cleanups` table separate
  length and lowercase checks. The four constraints are renamed
  `…_hash_length` → `…_hash_format` because they no longer check length alone.

## The pattern behind the ticket

There are three ways to declare a SHA-256 CHECK in `sessions/models.py`: the
`_lower_sha256_constraints` helper, hand-written paired dialect strings, and a
bare `length(x) = 64`. Nothing makes the helper the only way, so the weakest
form is also the shortest to type, and a new table copies whichever neighbour
the author looked at. The defect recurs by construction rather than by
carelessness; fixing instances does not stop the next one.

The leverage point is a whole-tree test, in the style of the existing AST and
shape gates, that walks both metadata objects and fails when a column in a
declared set of digest columns lacks both anchors on either dialect. The census
script is most of that test. It needs an explicit column inventory rather than
name matching, which is the decision it is waiting on (see below).

## Not fixed: 61 columns with no shape CHECK

These were identified, not changed. Whether each is a gap depends on facts not
established here (what the writer produces, whether NULL is meaningful), so
treat this as a worklist, not a defect list.

**Landscape, 47 columns.** 44 are `String(64)` and 3 are `String(32)`.
Measured: SQLite accepts a 200-character value into a `VARCHAR(64)` column, so
on the default Landscape store the declared width enforces nothing, and on
PostgreSQL it bounds the maximum only. 15 Landscape columns do carry the full
CHECK, all in the newer export, admission, coalesce and aggregation tables; the
core audit tables (`rows`, `node_states`, `calls`, `operations`, `nodes`,
`runs`, `sink_effects`, `sink_effect_members`, `artifacts`, `token_outcomes`,
`routing_events`) carry none. The three 32-character columns
(`nodes.source_file_hash`, `nodes.output_contract_hash`,
`run_sources.schema_contract_hash`) are not SHA-256 hex and would need their
own rule. Changing any of these is a Landscape epoch bump and a paired cutover.

**Sessions, 14 columns.** Nine are nullable provenance hashes
(`blobs.creating_*_hash`, `composition_proposals.*_hash`,
`interpretation_events.*_hash`, `composer_completion_events.payload_digest`,
`user_preferences.tutorial_source_data_hash`). Three are `NOT NULL` digests
(`library_entries.payload_digest`, `review_attestations.payload_digest`,
`skill_markdown_history.hash`). `web_instances.image_digest` is very likely an
OCI `sha256:<hex>` reference and `interpretation_events.hash_domain_version` is
a version label; neither fits the lowercase-hex rule.

One pair deserves first look: `interpretation_events.approved_prompt_artifact_hash`
and Landscape `calls.approved_prompt_artifact_hash` are the link between the two
stores (`docs/runbooks/staging-session-db-recreation.md`, epochs 57 and 41), and
neither side constrains the value's shape.

## Related, not fixed

The comment above `ck_blobs_ready_hash` in `sessions/models.py` cites
`sessions/schema.py:_validate_named_checks`. No such function exists; live
CHECK text is compared by `core/schema_shape.py:collect_metadata_shape_issues`.
The comment's claim (a same-named weaker CHECK is rejected at startup) is still
true, which is why this change needed no code beyond the epoch to reject older
stores.
