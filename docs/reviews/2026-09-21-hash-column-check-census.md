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

That leverage point now exists:
`tests/unit/architecture/test_digest_column_shape_checks.py` carries an explicit
inventory of every digest column with its shape, and fails when a digest-named
column is neither inventoried nor excluded with a reason, or when a listed
column's CHECKs stop rejecting malformed values. It is behavioural (it evaluates
the live SQLite CHECK expressions against probe values), so it does not care
which declaration style produced the constraint. Mutation-checked: restoring the
ticket's original `length(content_hash) = 64` turns it red, as does removing an
inventory entry.

## The 61 columns with no shape CHECK (second wave, session 63 / Landscape 43)

The first pass listed these as a worklist. They were then constrained, after
two measurements decided what each column really holds.

**Measured before constraining.** A read-only scan (`mode=ro&immutable=1`) of
430 real `audit.db` / `sessions.db` files on the development host tallied every
non-null value in these columns against the lowercase-hex rule. 49 columns held
nothing but lowercase hex of the declared width. The scan is also what found the
columns a blanket 64-hex rule would have broken:

| column | what it holds | rule |
|---|---|---|
| `token_outcomes.error_hash`, `token_work_items.pending_error_hash` | 16 hex | `_OptionalLowerHex16Check` (already used by `aggregation_result_members`) |
| `nodes.output_contract_hash`, `run_sources.schema_contract_hash` | 32 hex, `SchemaContract.version_hash()` | new `_OptionalLowerHex32Check` |
| `nodes.source_file_hash` | `sha256:` + 16 hex | new `_OptionalSha256Ref16Check` |
| `review_attestations.payload_digest`, `composer_completion_events.payload_digest` | `sha256:` + 64 hex | new `_prefixed_sha256_constraints` |
| `library_entries.payload_digest` | bare 64 hex (the payload store address) | `_lower_sha256_constraints` |

`payload_digest` is one column name carrying two shapes. Columns with no sample
data were constrained only after reading their writer.

**Landscape, 47 columns — all constrained.** SQLite accepts a 200-character
value into a `VARCHAR(64)` column (measured), so on the default Landscape store
the declared width enforced nothing, and on PostgreSQL it bounded the maximum
only. Before this, 15 Landscape columns carried a full CHECK, all in the newer
export, admission, coalesce and aggregation tables; the core audit tables
carried none.

**Sessions, 11 of 14 constrained.** Three are excluded, each with its reason
recorded in the gate: `interpretation_events.hash_domain_version` (a version
label), `web_instances.image_digest` (an OCI reference supplied by the platform)
and `user_preferences.tutorial_source_data_hash`. The last is echoed back by the
client with no server-side format validation, so a CHECK would turn a malformed
request into a 500; the honest fix is a validator at the request model, which is
not part of this change.

`interpretation_events.approved_prompt_artifact_hash` and Landscape
`calls.approved_prompt_artifact_hash` — the link between the two stores — now
carry the identical rule on both sides.

**Cost.** Production writers already conformed; what broke was tests seeding
placeholders such as `config_hash="test"`. Those now derive a real digest from a
readable label through `tests/fixtures/audit_hashing.py`
(`fake_sha256("config")`), which is also closer to the project's rule that a
fixture must have the shape its producer emits.

## Unwired column found on the way: `nodes.schema_hash`

Present since RC2 (`f4f348de1`, 2026-02-02) as a column plus an optional
registration parameter documented as an "input/output schema hash". No caller
has ever supplied it (NULL in 480 of 480 sampled node rows); the only write-site
change in its history removed a literal `schema_hash=None`. It was never
finished rather than removed, and it was superseded almost at once: `aa555c7cb`
added `input_contract_json` / `output_contract_json` the next day and
`84d296d5b` added `output_contract_hash`, which are populated. Nothing compares
or verifies it; `engine/orchestrator/export.py` asserts that it is `None`.

## Related, not fixed

The comment above `ck_blobs_ready_hash` in `sessions/models.py` cites
`sessions/schema.py:_validate_named_checks`. No such function exists; live
CHECK text is compared by `core/schema_shape.py:collect_metadata_shape_issues`.
The comment's claim (a same-named weaker CHECK is rejected at startup) is still
true, which is why this change needed no code beyond the epoch to reject older
stores.
