# Seam: session schema, epoch, and persistence (composer async operations)

Explorer lane, read-only. Tree: main checkout, `release/0.8.1` at `d479eb2b4`.
`src/` and `tests/` were clean at the start (`git status --short src tests` → empty).
**Caveat on anchors:** `CHANGELOG.md` is dirty in the working tree (`git diff --stat CHANGELOG.md` →
`54 insertions(+), 2 deletions(-)`, someone else's uncommitted edit) and `docs/` has untracked
files, so CHANGELOG and docs line numbers below are working-tree positions, not HEAD. All `src/`
and `tests/` anchors are HEAD == working tree.

Spec read in full: `docs/specs/2026-09-16-composer-async-operations-design.md` (315 lines).
The old plan `docs/plans/2026-09-20-composer-async-operations.md` is **stale on scope**: its goal
(lines 3-10) and §1 step 1 (line 67-68) still say five routes / "closed five-kind" checks and §2
step 1 says "The three guided routes use `guided_operation_request_hash` exactly"; the amended
spec (lines 6-12, 65-71) makes it freeform-only with a closed two-value `kind`.

---

## 1. Epoch: current value and every mirror

### 1.1 The constant and its lockstep twin

- `src/elspeth/web/sessions/models.py:366` — `SESSION_SCHEMA_EPOCH = 67`. History comment block
  `models.py:45-365`; the newest entry is `# 67:` at `models.py:361-365`.
- `src/elspeth/web/sessions/schema.py:40` — `_COORDINATION_HARD_CUT_EPOCH = 67`, with comment
  `schema.py:35-39`. Enforced by **exact equality** in `_validate_coordination_hard_cut_metadata`
  (`schema.py:519`): `schema.py:535-536`
  ```python
  if SESSION_SCHEMA_EPOCH != _COORDINATION_HARD_CUT_EPOCH:
      _schema_error("coordination schema epoch mismatch", expected=_COORDINATION_HARD_CUT_EPOCH, actual=SESSION_SCHEMA_EPOCH)
  ```
  A bump that forgets this constant stops every session DB from opening on any dialect.

### 1.2 Mirror inventory (union of the two commits that last moved 66→67)

The 66→67 bump was split across two commits; their union is the live mirror set.

`f5a770d3f chore(sessions): session schema epoch 67 ...` (2026-09-24) — 10 files:
`CHANGELOG.md`, `docs/runbooks/staging-session-db-recreation.md`, `src/elspeth/web/sessions/models.py`,
`src/elspeth/web/sessions/schema.py`, `tests/integration/web/composer/guided/test_schema9_epoch.py`,
`tests/unit/contracts/test_web_blob_fencing.py`, `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`,
`tests/unit/web/sessions/test_interpretation_events_table.py`,
`tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py`, `tests/unit/web/sessions/test_schema.py`.

`58582f847 fix(replay): integrate review corrections at session epoch 67` (54 files, mixed with a
Landscape change) moved the remaining doc mirrors: `README.md`, `docs/guides/sharing-pipelines.md`,
`docs/runbooks/aws-ecs-deployment.md`, `docs/runbooks/azure-container-apps-cold-install.md`,
`docs/runbooks/azure-container-apps-deployment.md`, `docs/runbooks/staging-session-db-recreation.md`,
`website/get-started.html` (plus the Landscape files).

Live literal mirrors of `67` at the current tree:

| File:line | Text |
|---|---|
| `src/elspeth/web/sessions/models.py:366` | `SESSION_SCHEMA_EPOCH = 67` (+ `# 67:` history at :361) |
| `src/elspeth/web/sessions/schema.py:40` | `_COORDINATION_HARD_CUT_EPOCH = 67` (+ comment :37-39) |
| `tests/integration/web/composer/guided/test_schema9_epoch.py:62` | `assert SESSION_SCHEMA_EPOCH == 67` (history comment :42-61) |
| `tests/unit/contracts/test_web_blob_fencing.py:3204` | `assert SESSION_SCHEMA_EPOCH == 67` |
| `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py:70,72` | `== 67`; `PRAGMA user_version ... == 67` |
| `tests/unit/web/sessions/test_interpretation_events_table.py:248` | `assert SESSION_SCHEMA_EPOCH == 67` |
| `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py:26` | `assert SESSION_SCHEMA_EPOCH == 67` |
| `tests/unit/web/sessions/test_schema.py:368` | `assert SESSION_SCHEMA_EPOCH == 67` (comment :355-367) |
| `CHANGELOG.md:9` (working tree) | `` `SESSION_SCHEMA_EPOCH` advances from 53\nto 67 `` — the line break is load-bearing (test below) |
| `CHANGELOG.md:38-42, 69, 72, 239` (working tree) | epoch-67 paragraph; "below epoch 67 (including epoch 66)"; "(session 67, Landscape 46)"; "(session epoch 67)" |
| `README.md:167-168` | "from session epoch 53\nto 67 and Landscape epoch 38 to 46" |
| `docs/guides/sharing-pipelines.md:73` | `` `SESSION_SCHEMA_EPOCH=67` `` |
| `docs/runbooks/staging-session-db-recreation.md:5, 7, 69-74, 217, 219, 837` | heading "(session epoch 67 ...)", "from 53 to 67", epoch-67 paragraph, "repair the epoch-67 release forward", "session-epoch-67/Landscape-epoch-46 record", `# expect 67 (== SESSION_SCHEMA_EPOCH)` |
| `docs/runbooks/aws-ecs-deployment.md:1621, 1623, 1649` | `"session_epoch": 67`, `"structural_changes": "session_epoch_35_to_67_landscape_epoch_29_to_46_..."`, "session epoch 67, Landscape epoch 46" |
| `docs/runbooks/azure-container-apps-deployment.md:109, 451, 464, 538` | "epoch-67 image (session epoch 67 ...)", "at session epoch 67" ×2, `"session_epoch": 67` |
| `docs/runbooks/azure-container-apps-cold-install.md:86` | "session epoch 67 and Landscape epoch 46 initialized;" |
| `website/get-started.html:106` | `<code>SESSION_SCHEMA_EPOCH</code> changes 53 → 67` plus a per-epoch narrative sentence list |

Derived (not literal, moves automatically): `src/elspeth/web/_acceptance_common/schema_facts.py:42`
(`"session_epoch": SESSION_SCHEMA_EPOCH`) and `:26-30` (`_SCENARIO_B_STRUCTURAL_CHANGES` f-string
embeds the live epoch — which is why the AWS runbook's literal `session_epoch_35_to_67_...` at :1623
must move). Historical/pinned, must NOT move: `tests/unit/web/acceptance_common/corpus/ecs_receipts.json`
(pins `session_epoch: 52`), and plan/review docs under `docs/plans/`, `docs/reviews/`.

Tests that make the doc mirrors gate-enforced (they fail if a doc is left behind):

- `tests/unit/docs/test_staging_session_recreation_policy.py:13` `test_current_cutover_and_verification_use_live_schema_epochs`
  and `:39` `test_replica_schema_cutover_belongs_to_0_8_1` (asserts `` f"`SESSION_SCHEMA_EPOCH` advances from 53\nto {SESSION_SCHEMA_EPOCH}" `` in the 0.8.1 CHANGELOG section — exact line break).
- `tests/unit/docs/test_release_version_surfaces.py:123` `test_operator_schema_version_examples_match_live_constants` (sharing-pipelines.md);
  also `test_scenario_b_runbook_record_matches_live_release_derivation` (AWS record == `_expected_schema_facts("B")`).
- `tests/unit/docs/test_readme_release_surface.py:21` `test_readme_operational_cutover_states_the_live_schema_epochs`.
- `tests/unit/web/test_azure_container_apps_runbook_contract.py:182` `test_every_epoch_literal_matches_the_live_constants`
  (every `session epoch (\d+)` and `"session_epoch": (\d+)` in the ACA runbooks must equal the constant).

A ready-made sweep list for the last bump exists at `docs/plans/2026-09-23-web-review-remediation/composer.md:50`
and the D8/T6 file list at `docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md:83, 660-729, 863-881`.
Precedent: the epoch bump is the **last commit** of the change (that plan's D8, line 83).

### 1.3 Instruments and controls

- Single-line scan: `grep -rniE "epoch.{0,80}\b67\b|\b67\b.{0,80}epoch" src tests deploy scripts frontend CHANGELOG.md docs website ...`
  (working tree). Known-positive: matched `models.py:366`. It missed cross-line forms, so a second
  instrument was used.
- Multi-line scan: Python regex over `git ls-files` (tracked only) for `epoch…\n?…67`, `53\s*\n?\s*to\s+67`,
  `53 → 67`, `user_version…67`. Known-positive: `CHANGELOG.md:9` (`53\nto 67`, cross-line) and
  `website/get-started.html:106` (`53 → 67`) — both missed by the single-line scan. Known over-match,
  inspected and excluded: `docs/architecture/adr/048-required-coordination-token-for-landscape-mutations.md:625`
  (a table row numbered `| 67 |` next to "leader epoch CAS").
- `deploy/` negative: `grep -rn "epoch\|EPOCH" deploy` hit only `deploy/azure-container-apps/scripts/acceptance.sh:658-668`
  (Unix `date +%s` epochs) — the instrument works there and there is no session-epoch mirror in `deploy/`.
  `deploy/elspeth-web.env` (the served config per memory) is untracked and gitignored
  (`.gitignore:302:deploy/**/*.env`); not read.
- Frontend: `grep -rn "SESSION_SCHEMA_EPOCH\|session_epoch\|schemaEpoch" frontend/src` → no hits
  (no positive control available in that tree; treat as "not found").

### 1.4 Deploy rule

An epoch bump means the deployed `sessions.db` is refused at boot (`SessionSchemaError`, schema.py
`_assert_on` :316-368). Operator procedure = move the session DB aside (`auth.db` untouched), restart
to create a fresh store, then `elspeth composer users bootstrap-admin local <user> --note "..."`
(`docs/runbooks/staging-session-db-recreation.md:254-256`, section "Every local account lands
`pending` after this reset" at :232). The runbook's "Current Cutover" section must name the new epoch
(pinned by the tests above). The new-table bump is a structural change on top of 67, so it becomes 68
unless another lane lands first — recheck the tip immediately before the bump commit.

---

## 2. Schema identity / "fingerprint" mechanism

There is **no structural schema fingerprint/hash**. Control: `grep -c fingerprint` → 0 in
`src/elspeth/core/schema_identity.py` and `src/elspeth/core/schema_shape.py`; 1 in
`src/elspeth/web/sessions/schema.py` and it is the prose comment at :38. The positive control for the
same grep is `grep -c schema_epoch src/elspeth/core/schema_identity.py` → 11.

The identity/shape proof is five layers, all driven by `sessions.models.metadata`:

1. SQLite header sentinels: stamped `schema.py:292-294` (`PRAGMA application_id`, `PRAGMA user_version = {SESSION_SCHEMA_EPOCH}`),
   asserted `schema.py:320-343`.
2. Cross-dialect identity row `elspeth_schema_identity` (store_kind `"session"`, epoch): stamped
   `schema.py:295-303`, asserted `schema.py:345-368` via `read_schema_identities` / `schema_identity_mismatch`
   (`core/schema_identity.py:61, 86`). Table built by `create_schema_identity_table` (`core/schema_identity.py:27`),
   bound at `models.py:487`. Writer is `SessionSchemaAuthority` (`schema.py:249`), policy
   `TablePolicy("elspeth_schema_identity", "global", "SessionSchemaAuthority")`.
3. Full reflection comparison: `_validate_current_schema` (`schema.py:484`) → table-set equality
   (`schema.py:497-504`) → `collect_metadata_shape_issues` (`core/schema_shape.py:606`). CHECKs are
   compared by name and parsed-expression AST equivalence (`_collect_check_issues`,
   `core/schema_shape.py:1141-1183`): declared SQL is compiled per dialect with `literal_binds`, reflected
   SQL is parsed and compared. This is where PostgreSQL deparse quirks bite (see §3.3).
4. Trigger inventory: `_REQUIRED_AUDIT_TRIGGERS` (`schema.py:97-114`) validated by
   `SessionSchemaAuthority.validate_required_triggers` (`schema.py:370-433`); the PG branch is a
   **hand-written per-relation `relname`/`tgname` query** (`schema.py:381-411`, guided_operations arm at :406-410).
5. Partial-index dialect symmetry: `_validate_partial_index_dialect_symmetry` (`schema.py:573`), run
   un-guarded by `probe_current_schema` (`schema.py:185-216`). Any partial index on the new table must
   set both `sqlite_where=` and `postgresql_where=` compiling to identical text.

Also: `_validate_coordination_hard_cut_metadata` (`schema.py:519-555`) pins `_COORDINATION_HARD_CUT_TABLES`
(`schema.py:41-53`) and expiry indexes. The new table need not join that set, but if it does,
`tests/unit/web/sessions/test_schema.py:369-384` asserts `expected_tables == _COORDINATION_HARD_CUT_TABLES`.

Tests: `tests/unit/core/test_schema_identity.py`, `tests/unit/core/test_schema_shape.py`,
`tests/unit/web/sessions/test_schema.py`, `tests/testcontainer/web/test_schema_probe_postgres.py:108`
`test_fresh_create_reaches_current` (PG fresh create must probe CURRENT — the reflection round-trip gate),
`:354` identity stamp, `:397` identity drift, `:323` drifted CHECK names itself.

---

## 3. Template table: `guided_operations`

### 3.1 Full definition — `src/elspeth/web/sessions/models.py:1043-1231`

```python
# One bounded reservation row per client-authored mutating action. Raw request
# bodies, user intent, and provider errors are deliberately absent: replay is
# bound by a canonical request hash and a small immutable result locator.
guided_operations_table = Table(
    "guided_operations",
    metadata,
    Column("session_id", String(128), nullable=False),
    Column("operation_id", String(128), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("lease_token", String(256), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("attempt", Integer, nullable=False),
    Column("originating_message_id", String(128), nullable=True),
    Column("proposal_id", String(128), nullable=True),
    Column("result_kind", String(32), nullable=True),
    Column("result_state_id", String(128), nullable=True),
    Column("result_message_id", String(128), nullable=True),
    Column("result_session_id", String(128), nullable=True),
    Column("response_hash", String(64), nullable=True),
    Column("failure_code", String(128), nullable=True),
    Column("unproducible_output_fields", JSON(none_as_null=True), nullable=True),
    Column("failure_diagnostics", JSON(none_as_null=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=True),
    PrimaryKeyConstraint("session_id", "operation_id", name="pk_guided_operations"),
    UniqueConstraint("session_id", "operation_id", "request_hash", name="uq_guided_operations_request_binding"),
    ForeignKeyConstraint(["session_id"], ["sessions.id"], name="fk_guided_operations_session", ondelete="CASCADE"),
    ForeignKeyConstraint(["originating_message_id", "session_id"], ["chat_messages.id", "chat_messages.session_id"],
                         name="fk_guided_operations_originating_message_session", ondelete="RESTRICT"),
    ForeignKeyConstraint(["proposal_id", "session_id"], ["composition_proposals.id", "composition_proposals.session_id"],
                         name="fk_guided_operations_proposal_session", ondelete="RESTRICT"),
    ForeignKeyConstraint(["result_state_id", "session_id"], ["composition_states.id", "composition_states.session_id"],
                         name="fk_guided_operations_result_state_session", ondelete="RESTRICT"),
    ForeignKeyConstraint(["result_message_id", "session_id"], ["chat_messages.id", "chat_messages.session_id"],
                         name="fk_guided_operations_result_message_session", ondelete="RESTRICT"),
    ForeignKeyConstraint(["result_session_id"], ["sessions.id"], name="fk_guided_operations_result_session", ondelete="RESTRICT"),
    CheckConstraint("length(operation_id) >= 1 AND length(operation_id) <= 128", name="ck_guided_operations_operation_id_bounded"),
    # ... five "<ref> IS NULL OR (length(<ref>) >= 1 AND length(<ref>) <= 128)" bounded-id CHECKs (:1104-1123)
    CheckConstraint("kind IN ('guided_start', 'guided_respond', 'guided_chat', 'guided_convert', 'guided_reenter', 'guided_plan', 'state_revert', 'session_fork')",
                    name="ck_guided_operations_kind"),                                               # :1124-1127
    CheckConstraint("status IN ('in_progress', 'completed', 'failed')", name="ck_guided_operations_status"),  # :1128
    CheckConstraint(_lower_sha256_check("request_hash", dialect="sqlite"), name="ck_guided_operations_request_hash").ddl_if(dialect="sqlite"),
    CheckConstraint(_lower_sha256_check("request_hash", dialect="postgresql"), name="ck_guided_operations_request_hash").ddl_if(dialect="postgresql"),
    CheckConstraint("attempt >= 1", name="ck_guided_operations_attempt"),
    CheckConstraint("updated_at >= created_at", name="ck_guided_operations_updated_after_created"),
    CheckConstraint("settled_at IS NULL OR settled_at >= created_at", name="ck_guided_operations_settled_after_created"),
    CheckConstraint("lease_token IS NULL OR (length(lease_token) >= 1 AND length(lease_token) <= 256)", name="ck_guided_operations_lease_token_bounded"),
    CheckConstraint("result_kind IS NULL OR result_kind IN ('composition_state', 'pipeline_proposal', 'session', 'declined')", name="ck_guided_operations_result_kind"),
    CheckConstraint("failure_code IS NULL OR failure_code IN ('provider_unavailable', 'provider_timeout', 'invalid_provider_response', "
                    "'cost_unavailable', 'planner_repair_exhausted', 'policy_blocked', 'admission_refused', 'stale_conflict', "
                    "'integrity_error', 'custody_error', 'quota_exceeded', 'operation_failed', 'request_cancelled')",
                    name="ck_guided_operations_failure_code"),                                       # :1146-1151
    # JSON shape CHECKs, dialect-paired (:1152-1178):
    CheckConstraint("unproducible_output_fields IS NULL OR (json_type(unproducible_output_fields) = 'array' AND json_array_length(unproducible_output_fields) > 0)",
                    name="ck_guided_operations_unproducible_output_fields_shape").ddl_if(dialect="sqlite"),
    CheckConstraint("unproducible_output_fields IS NULL OR (json_typeof(unproducible_output_fields) = 'array'::text AND json_array_length(unproducible_output_fields) > 0)",
                    name="ck_guided_operations_unproducible_output_fields_shape").ddl_if(dialect="postgresql"),
    # (same pair for failure_diagnostics, bounded <= 32)
    CheckConstraint(   # status bundle, :1179-1192
        "(status = 'in_progress' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND settled_at IS NULL AND result_kind IS NULL AND result_message_id IS NULL AND response_hash IS NULL AND failure_code IS NULL "
        "AND unproducible_output_fields IS NULL AND failure_diagnostics IS NULL) OR "
        "(status = 'completed' AND lease_token IS NULL AND lease_expires_at IS NULL "
        "AND settled_at IS NOT NULL AND result_kind IS NOT NULL AND response_hash IS NOT NULL AND failure_code IS NULL "
        "AND unproducible_output_fields IS NULL AND failure_diagnostics IS NULL) OR "
        "(status = 'failed' AND lease_token IS NULL AND lease_expires_at IS NULL "
        "AND settled_at IS NOT NULL AND result_kind IS NULL AND result_state_id IS NULL "
        "AND result_message_id IS NULL AND result_session_id IS NULL AND proposal_id IS NULL "
        "AND response_hash IS NULL AND failure_code IS NOT NULL)",
        name="ck_guided_operations_status_bundle"),
    CheckConstraint("... kind-bound result locator ...", name="ck_guided_operations_result_locator"),  # :1193-1213
    CheckConstraint("response_hash IS NULL OR (length(response_hash) = 64 AND response_hash NOT GLOB '*[^a-f0-9]*')",
                    name="ck_guided_operations_response_hash").ddl_if(dialect="sqlite"),
    CheckConstraint("response_hash IS NULL OR (length(response_hash) = 64 AND response_hash ~ '^[a-f0-9]+$')",
                    name="ck_guided_operations_response_hash").ddl_if(dialect="postgresql"),
)
Index("ix_guided_operations_status_lease", guided_operations_table.c.status, guided_operations_table.c.lease_expires_at)  # :1223
Index("uq_guided_operations_active_proposal_admission", guided_operations_table.c.session_id, guided_operations_table.c.proposal_id,
      unique=True,
      sqlite_where=(guided_operations_table.c.status == "in_progress") & guided_operations_table.c.proposal_id.is_not(None),
      postgresql_where=(guided_operations_table.c.status == "in_progress") & guided_operations_table.c.proposal_id.is_not(None))  # :1224-1231
```

(Lines marked `...` are abridged in this file only; full text at the cited lines.)

Siblings: `guided_operation_admission_blocks_table` (`models.py:1011-1041`) and
`guided_operation_events_table` (`models.py:1236-1283`, composite FK
`(session_id, operation_id, request_hash)` → `guided_operations` ON DELETE CASCADE). The spec does not
ask for an events table for the new job.

Reusable CHECK helpers in `models.py`: `_sql_non_blank_text` (:377), `_non_blank_text_constraints` (:383),
`_lower_sha256_check` (:400), `_lower_sha256_constraints` (:407), `_prefixed_sha256_constraints` (:423).
Each returns a SQLite/PG pair via `.ddl_if(dialect=...)`.

A bounded-JSON-as-Text precedent (validated on read against its canonical re-dump) is
`composer_progress_snapshots_table` (`models.py:2749-2762`: `Column("snapshot_json", Text, nullable=True)`,
`CheckConstraint("snapshot_json IS NULL OR length(snapshot_json) <= 16384", name="ck_composer_progress_bounded")`)
read back by `_read_snapshot` (`coordination/composer_progress_authority.py:85-91`:
`snapshot.model_dump_json() != row.snapshot_json → RuntimeError("... is not canonical")`).

### 3.2 Terminal immutability trigger (spec line 90: "The completed payload and hash are immutable")

guided uses a trigger, not only a CHECK:
- PG: `PostgresqlAuditDDL(table=guided_operations_table, trigger_name="trg_guided_operations_terminal_immutable", ...)`
  in `POSTGRESQL_AUDIT_DDL_COHORT` (`models.py:1861`, entry at :2003-2027; class `PostgresqlAuditDDL` :1846),
  installed by the loop at `models.py:2447-2457`.
- SQLite: `event.listen(guided_operations_table, "after_create", DDL("CREATE TRIGGER IF NOT EXISTS trg_guided_operations_terminal_immutable BEFORE UPDATE ON guided_operations FOR EACH ROW WHEN OLD.status IN ('completed', 'failed') BEGIN SELECT RAISE(ABORT, ...); END;").execute_if(dialect="sqlite"))`
  at `models.py:2330-2342`.

Adding an equivalent trigger to the new table touches **five** places, each with an exact-set test:
1. `models.py` `POSTGRESQL_AUDIT_DDL_COHORT` entry + SQLite `event.listen`.
2. `schema.py:97-114` `_REQUIRED_AUDIT_TRIGGERS`.
3. `schema.py:381-411` the hard-coded PG `relname`/`tgname` arms (a new `OR (relation.relname = 'composer_async_operations' AND ...)`).
4. `tests/unit/web/sessions/test_schema.py:593-609` (mock-engine PG DDL list).
5. `tests/testcontainer/web/test_schema_probe_postgres.py:962-1000` (`assert names == {...}` — **exact equality over all session triggers**).

Note the spec requires request JSON to be *cleared* on terminal settlement; that happens in the
`running → completed/failed` UPDATE itself, which a `WHEN OLD.status IN (terminal)` trigger permits.

### 3.3 PostgreSQL reflection quirks the new CHECKs must respect

- JSON type CHECKs: PG deparses `json_typeof(x) = 'array'` with a cast; the declared text must be
  `'array'::text` or the probe classifies a fresh DB as non-current (comment at `models.py:1157-1161`).
  If the new request/result columns use `JSON` with a `json_type(...) = 'object'` CHECK, copy this
  pattern (`'object'::text` on the PG arm).
- One-element `IN (...)` is reflected by PG as `=` (elspeth-d0e62aea41, cited in AGENTS.md); write a
  single-value CHECK as `=` (guided's admission block does: `"kind = 'guided_start'"`, `models.py:1031`).
  The spec's two-value `kind IN ('compose_message', 'compose_recompose')` is fine.
- Guard: `test_fresh_create_reaches_current` (`test_schema_probe_postgres.py:108`) is the gate; it only
  runs under `-m testcontainer -n 0`.

### 3.4 Commit that introduced `guided_operations`

`git log --reverse -S 'guided_operations_table = Table(' -- src/elspeth/web/sessions/models.py` →
`73b497cdf 2026-07-18 feat(composer): allocate guided operation schema`. Files touched (8):

```
src/elspeth/web/sessions/models.py                                 | 246 +++
src/elspeth/web/sessions/schema.py                                 |   6 +
tests/integration/web/composer/guided/test_guided_operations_schema.py | 263 +++ (new)
tests/integration/web/composer/guided/test_schema8_epoch.py        |  27 +++ (new)
tests/testcontainer/web/test_schema_probe_postgres.py              |  43 ++++
tests/unit/web/sessions/test_blob_inline_resolutions_schema.py     |   6 +-
tests/unit/web/sessions/test_interpretation_events_table.py        |   6 +-
tests/unit/web/sessions/test_schema.py                             |   2 +
```

That commit predates the whole-tree gates in §6 below; a table added today also needs those.
`tests/integration/web/composer/guided/test_guided_operations_schema.py` is the best test template:
`test_tables_and_composite_keys_are_current` (:178), `test_status_bundles_are_exact` (:428),
`test_terminal_operation_allows_settlement_once_then_is_immutable` (:662),
`test_operation_id_is_scoped_by_session` (:592), `test_startup_probe_rejects_missing_required_trigger` (:769),
`test_schema_has_no_raw_request_or_lease_token_event_columns` (:776).

---

## 4. Service API and write authority for `guided_operations`

### 4.1 Protocol surface (`src/elspeth/web/sessions/protocol.py`)

- `GuidedOperationKind = Literal[...]` :167-176; `GuidedOperationFailureCode` :191; closed value sets
  `GUIDED_OPERATION_KIND_VALUES` / `GUIDED_OPERATION_FAILURE_CODE_VALUES` :306-307 (paired-contract rule:
  a Literal extension ships with the CHECK and an epoch bump, comment :177-190).
- `GuidedOperationFence(session_id: UUID, operation_id: str, lease_token: str, attempt: int)` :386-401.
- Outcome union `GuidedOperationClaimed | GuidedOperationTakenOver | GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed` :551-617.
- `GuidedOperationConflictError(*, session_id: UUID, operation_id: str)` :620 (routes map to 409);
  `GuidedOperationFenceLostError(fence)` :636 (never retains the lease token).
- `SessionServiceProtocol` :3958. Guided methods: `reserve_guided_operation` :3979, `get_guided_operation` :3991,
  `renew_guided_operation` :4018, `bind_guided_operation`, `complete_guided_operation` :4038,
  `fail_guided_operation`, `fail_guided_operation_with_audit` :4058. Properties
  `session_operation_authority`, `session_operation_owner_instance_id`, `session_operation_lease_seconds` :3961-3967.

### 4.2 Writers (`src/elspeth/web/sessions/service.py` unless noted)

`grep -rn "guided_operations_table\b" src` (excluding models.py) → writers only in
`sessions/service.py` and `coordination/repository.py`; readers also in `web/blobs/service.py`.

- Reservation/takeover: `SessionServiceImpl.reserve_guided_operation` :5459-5602. Sync body opens
  `self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid)` (:5477), reads
  DB time (:5478), re-checks the **session-operation fence** (:5479-5485, COMPOSE or SESSION_FORK),
  inserts at :5509-5521, takeover CAS at :5567-5589:
  ```python
  changed = conn.execute(
      update(guided_operations_table).where(
          guided_operations_table.c.session_id == sid,
          guided_operations_table.c.operation_id == operation_id,
          guided_operations_table.c.status == "in_progress",
          guided_operations_table.c.lease_token == prior_token,
          guided_operations_table.c.attempt == prior_attempt,
          guided_operations_table.c.lease_expires_at <= now,
      ).values(lease_token=lease_token, lease_expires_at=lease_expires_at, attempt=next_attempt, updated_at=now)
  ).rowcount
  if changed != 1:
      raise AuditIntegrityError("Guided operation takeover lost its locked compare-and-swap")
  ```
  Mismatch → `GuidedOperationConflictError` when `row["kind"] != kind or row["request_hash"] != request_hash` (:5539-5540).
  **Guided reservation requires a live COMPOSE `SessionOperationContext`**; the new async queue insert happens
  before 202 and must not wait for a compose lease (spec §2 line 124-125), so it cannot reuse this entry shape.
- Live fence check: `require_guided_operation_fence_on_connection` :5854-5886 (asserts write lock held :5861,
  reads row, compares status/token/attempt/expiry vs DB time) and `require_guided_operation_authority_on_connection`
  (:4967-4983) = guided fence + session-operation fence in one call.
- Renewal CAS: `renew_guided_operation` :5889-5935 (`... lease_expires_at > now` predicate, `rowcount != 1 → GuidedOperationFenceLostError`).
- Terminal CAS: `_GuidedSessionMutations.complete` :3989-4084 and `.fail` :4086-... — both re-run
  `_require_exact()` immediately before the terminal `update(...).where(status=='in_progress', lease_token, attempt, lease_expires_at > now)`
  and raise on `rowcount != 1` (complete: :4033-4056), then append an event row, in the caller's transaction.
- Dual-fence capability: `_GuidedSessionMutationState` :3787-3822 (holds connection + guided fence + session
  context; `_require_exact` re-validates both), `_GuidedSessionMutations` :3826, `_GuidedSessionMutationTransaction`
  :4356-4406, yielded by `_guided_session_mutation_transaction` :5140-5166. **This is the template for the
  spec's "both the existing `SessionOperationLease` context and the transport job's live running fence"**
  (spec lines 98-100). Today ordinary composer writes carry only the session fence (§5.2).
- Other writers: `coordination/repository.py` `_ForkParentGuidedMutations.bind_guided_fork` :4198;
  `sessions/service.py` fork insert :14468.

### 4.3 Authority policy the new table must be registered under

`tests/unit/architecture/test_session_db_mutation_authority.py`:
- `TablePolicy("guided_operations", "session", "GuidedSessionMutationAuthority", (("SessionForkParentGuidedMutations", {"update"}), ("GuidedSessionAdmissionAuthority", {"insert","update"}), ("SessionForkAuthority", {"insert"})))` at :181-189.
- `AuthoritySymbol` bindings for the guided writers at :588-611 (e.g. `SessionServiceImpl.reserve_guided_operation → GuidedSessionAdmissionAuthority`).
- `_REVIEWED_WRITERS` entries with AST fingerprints, e.g. :2702-2720 (`reserve_guided_operation._sync` insert `fp=1161702a3f59ea98 line=5509`, update line=5568).
- Precedent for a newer, separately-owned lease table: `TablePolicy("composer_inflight_requests", "session", "SessionComposerProgressAuthority")` (:79-80) written by `SessionComposerProgressAuthority` (`coordination/composer_progress_authority.py:135`, "Sole writer of the durable Composer progress and inflight tables").

---

## 5. Unit-of-work pattern for the compose turn (where a terminal job write could join)

### 5.1 Transaction primitives (`sessions/service.py`)

- `_run_sync` :4692 → `run_sync_in_worker` (thread pool). All DB work is sync inside a worker.
- `_session_process_locked_begin(session_id)` :4758-4762: `process_session_lock(...)` then `self._engine.begin()`.
  On SQLite `engine.begin()` issues `BEGIN IMMEDIATE` (`sessions/engine.py:116-137`), so a write transaction
  takes the DB write lock up front.
- `_session_write_lock(conn, session_id)` :4800-4825: SQLite → `sqlite_transaction_session_lock` (process RLock);
  PostgreSQL → `_acquire_session_advisory_lock` :4711-4751 = existing `pg_advisory_xact_lock(classid, hashtext(session_id))`.
  (Spec "No PostgreSQL advisory lock is added" — this existing session write lock is unchanged.)
- `_guided_database_now(conn)` :4827-4845: SQLite `SELECT CURRENT_TIMESTAMP`, PG `SELECT clock_timestamp()`.
- `_require_session_operation_context_on_connection` :4931-4965: selects `session_operation_fences` by
  `(session_id, operation_id, lease_token, operation_epoch, operation_kind, released_at IS NULL, lease_expires_at > now)`.
- `_session_composer_mutation_transaction(conn, *, session_id, session_operation_context, expected_kind)` :4986-5012
  — every ordinary composer write re-checks the session fence under the held lock.

### 5.2 The freeform success path is **three** write transactions plus a read, not one

`POST /messages` (`routes/messages.py`, `send_message` registered at :112-125):
- lease: `compose_lock` :144, `SessionOperationLease.acquire(... COMPOSE ...)` :147.
- user row: `service.add_message_with_transcript(...)` :238.
- intermediate tool-call turns: compose loop → `persist_compose_turn_async` (service.py:7006) → `persist_compose_turn`
  (service.py:6650-7004), single transaction per turn: process lock + write lock + composer mutation tx
  (:6751-6760), stale-state guard (:6772-6785), assistant + tool rows + composition states + rejection events.
- final publication, **separate transactions**:
  1. state: `service.save_composition_state(..., provenance="post_compose")` :933 (or `commit_transition_response` :922/:963 on the guided-terminal branch);
  2. assistant: `service.add_message(...)` :978 (service.py:9369; its own `_sync` tx at :9467-9478);
  3. audit cohort: `_persist_turn_audit_cohort(...)` :994 → `service.add_messages_atomic` (service.py:14706) (`routes/_helpers.py:1988-2066`);
  4. read: `_pending_proposal_responses(service, session.id)` :1027 (`_helpers.py:454`), then `MessageWithStateResponse(...)` :1028.
- proposal rows are **written mid-loop, not at final publication**: `composition_proposals` inserts exist only in
  `_SessionComposerMutations` (service.py:3220) methods `create_composition_proposal` (:3228, insert :3270), `create_pipeline_composition_proposal` (:3297, insert :3388)
  and `create_guided_pipeline_proposal` (:3426, insert :3465) (control: `grep -n "insert(composition_proposals_table)"`
  over service.py and coordination/repository.py → exactly these three, all in service.py). The response's `proposals`
  list is a snapshot read of pending rows after the publication writes.
- `POST /recompose` (`routes/composer/compose.py`): same shape — lease :112/:115; `commit_transition_response` :665/:706;
  `save_composition_state` :676; `add_message` :721; `_persist_turn_audit_cohort` :734; proposals read :760; response :761.

The only existing single-transaction **state + assistant** primitive is
`commit_composition_response` (service.py:10134-10200: stale-head check, `_insert_composition_state`,
`_insert_transition_assistant` in one tx). Its only caller is `commit_transition_response`
(service.py:10106-10132, requires `guided_session.transition_consumed=true`). Control:
`grep -rn commit_composition_response src` → the def (:10134), the protocol decl (protocol.py:4733),
and the single call at service.py:10125; no freeform caller.

Consequence for the spec (§4 lines 254-256: "Persist the public final response and transport terminal in
the same session transaction as the final assistant/result publication"): there is no existing freeform
transaction to join. The plan must either (a) build a new combined write (state? + assistant + audit
cohort + job terminal CAS) in one transaction, or (b) define "final publication" as one of these and
accept that earlier publication rows can be durable without a terminal job (the spec already allows
"Earlier audit or user rows may remain after failure", line 255-256). Also note the response's
`proposals` list is a **read after the writes** (`_pending_proposal_responses`), so the DTO the job stores
must be built from reads inside, or after, the terminal transaction — the spec says "prepare the validated
public response before its final commit" (lines 61-62).

---

## 6. Whole-tree gates a new session table trips

1. `tests/unit/architecture/test_session_db_mutation_authority.py:11534`
   `test_sessions_metadata_table_policy_is_exact_and_protected` — hard `assert live_tables == reviewed_tables`
   (`Counter(sessions_metadata.tables)` vs `_TABLE_POLICIES`). **Measured at HEAD: PASS.** A new table
   with no `TablePolicy` turns it red.
2. Same file :19034 `test_all_production_sessions_writers_are_reviewed_typed_authorities` — **measured at
   HEAD: XFAIL** ("Unexpected/unreviewed (83)", "Unresolved write executions (44)", "Stale reviewed read
   connections (8)"). New unreviewed writers extend the xfail rather than failing it, so compare the drift
   before/after; add `AuthoritySymbol` + `_REVIEWED_WRITERS` entries (fingerprint + line) for the new
   writers. Also :11654 `test_named_authority_registry_is_explicit_extensible_and_exact` asserts
   (:11733-11736) every `_NAMED_AUTHORITY_SYMBOLS` authority appears in some policy, and pins
   `policies["guided_operations"].operation_authorities` exactly (:11720-11724) — do not widen it.
   Also noted at `membership_authority.py:57-59`: "the writer manifest treats a connection passed to any
   callable as an escaped handle" — each authority method must read the clock through its own connection.
   Command/measurement: `.venv/bin/python -m pytest -n 0 -p no:cacheprovider tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities ...::test_sessions_metadata_table_policy_is_exact_and_protected -rxX`
   → `exit=0`, `1 passed, 1 xfailed in 153.20s`.
3. `tests/unit/architecture/test_digest_column_shape_checks.py` — `_DIGEST_NAME` (:46) matches
   `(^|_)(hash|sha256|digest|fingerprint|checksum)(_|$)|_hex$`. `request_hash`, `result_hash`/`response_hash`
   etc. fail until listed in `_INVENTORY` (guided entry at :85: `("sessions", "guided_operations"): {"request_hash": "hex64", "response_hash": "hex64"}`)
   with a lowercase-hex CHECK. PG arm proven by `test_schema_probe_postgres.py:194`
   `test_postgres_digest_checks_enforce_every_inventoried_shape` (iterates the same inventory).
4. Trigger inventory tests (§3.2) if a terminal-immutability trigger is added.
5. `config/cicd/soft-mapping-census.yaml` (generated by `python scripts/check_contracts.py --write-census`;
   header :1-9) counts every `dict/Mapping[str, Any|object]` annotation per file. Copying
   `guided_operation_request_hash`'s `normalized: dict[str, Any]` (guided_operations.py:30) into a new module
   adds a census row (`guided_operations.py` is at :1021 with `dict[str, Any]: 1`); re-pin the census in the same commit.
6. `tests/unit/web/sessions/test_schema.py:369-384` only if the table joins `_COORDINATION_HARD_CUT_TABLES`.

---

## 7. Session deletion cascade

- The only hard delete of a session: `_SessionOperationAuthorityRepository.archive_delete` —
  `conn.execute(delete(sessions_table).where(sessions_table.c.id == fence.session_id))`
  (`coordination/repository.py:5650-5665`), under an ARCHIVE fence CAS. Control: `grep -rn "delete(sessions_table)" src` → exactly that one hit.
- Child removal is by FK `ON DELETE CASCADE`; SQLite enforces FKs via `PRAGMA foreign_keys=ON`
  (`sessions/engine.py:103`), probed at boot (`engine.py:150-157`).
- **But archive refuses while a guided op is active:** `decide_and_soft_archive` (`coordination/repository.py:718`)
  selects any `guided_operations.status == 'in_progress'` for the session (:729-744) and raises
  `SessionGuidedOperationInProgressError` (protocol.py:649); also refuses an incoming active fork (:745-757).
  Completed/failed fork operations count as durable history → soft archive (`archived_at`) instead of delete (:758-775).
- Soft-archived sessions keep all rows; guided reservation refuses an archived parent
  (`reserve_guided_operation`, service.py:5502-5505; takeover path :5543-5547 treats it as Tier-1 breach).
- The spec says "Session deletion removes the job" (line 84-85) — a `sessions.id` FK with `ondelete="CASCADE"`
  gives that. Whether archive should also *refuse* while a job is `queued`/`running` (mirroring guided) is an
  open decision; a COMPOSE lease already excludes ARCHIVE during `running`, but a `queued` job holds no lease.
- An `actor_user_id` FK choice: `sessions.user_id` → `identities.identity_id ON DELETE RESTRICT` (models.py:494);
  `composer_inflight_requests.identity_id` uses `ON DELETE CASCADE` (models.py:2739). **`guided_operations`
  has no actor column at all** (columns at models.py:1049-1069); `actor` exists only on
  `guided_operation_admission_blocks` (:1018) and `guided_operation_events` (:1243), and
  `reserve_guided_operation(actor=...)` routes it into the event row. Guided replay binds only
  `kind` + `request_hash` (service.py:5539-5540). So the spec's "returned only when kind, actor, and hash all
  agree" (spec line 111) is a new row-level binding with no guided precedent; the FK mode (RESTRICT like
  `sessions.user_id`, CASCADE like `composer_inflight_requests`, or none) is an open decision.

---

## 8. Canonical JSON / SHA-256 helpers and `guided_operation_request_hash`

- `src/elspeth/contracts/hashing.py`: `CANONICAL_VERSION = "sha256-rfc8785-v1"` (:28);
  `is_lower_sha256_hex(value: object) -> TypeGuard[str]` (:32); `canonical_json(obj: Any) -> str` (:66,
  normalizes Mapping/tuple, rejects NaN/Inf/frozenset, `rfc8785.dumps`); `stable_hash(obj: Any) -> str` (:89,
  `sha256(canonical_json(obj).encode("utf-8")).hexdigest()`). `core/canonical.py:197, 218` wrap these with
  pandas/numpy normalization (not needed for DTO JSON).
- Full source, `src/elspeth/web/sessions/guided_operations.py:1-44`:
  ```python
  """Canonical codecs for retry-safe guided operations."""
  from __future__ import annotations
  from typing import Any
  from uuid import UUID
  from pydantic import BaseModel
  from elspeth.contracts.errors import AuditIntegrityError
  from elspeth.contracts.hashing import stable_hash
  from elspeth.web.sessions.protocol import GuidedOperationKind

  _GUIDED_OPERATION_REQUEST_SCHEMA = "guided-operation-request.v1"

  def guided_operation_request_hash(*, session_id: UUID, kind: GuidedOperationKind, request: BaseModel) -> str:
      """Bind one strict request DTO to a session, excluding its retry id.

      Defaults and explicit ``None`` values are materialized so omitted and
      explicit-default requests share one canonical replay identity. The client
      operation id is transport retry state, not request semantics.
      """
      config = type(request).model_config
      if "strict" not in config or config["strict"] is not True or "extra" not in config or config["extra"] != "forbid":
          raise AuditIntegrityError("Guided operation hashing requires a strict, extra-forbid request DTO")
      if "operation_id" not in type(request).model_fields:
          raise AuditIntegrityError("Guided operation request DTO is missing operation_id")
      normalized: dict[str, Any] = request.model_dump(
          mode="json", exclude={"operation_id"}, exclude_unset=False, exclude_defaults=False, exclude_none=False,
      )
      return stable_hash({"schema": _GUIDED_OPERATION_REQUEST_SCHEMA, "session_id": str(session_id), "kind": kind, "request": normalized})
  ```
  Call sites: `routes/guided_operations.py:516`, `routes/composer/guided.py:1481`, `sessions/service.py:1254, 14453`.
- Response hash model: `guided_response_hash(response: BaseModel) -> str` (`routes/guided_operations.py:160-170`):
  requires strict + extra-forbid, re-validates `type(response).model_validate(response.model_dump(mode="python"), strict=True)`,
  returns `stable_hash(strict_response.model_dump(mode="json"))`; replay verifies at :198-199
  (`AuditIntegrityError` on mismatch).
- DTO compatibility:
  - `MessageWithStateResponse(_StrictResponse)` (`sessions/schemas.py:228-237`: `message: ChatMessageResponse`,
    `state: CompositionStateResponse | None = None`, `proposals: list[CompositionProposalResponse]`);
    `_StrictResponse.model_config = ConfigDict(strict=True, extra="forbid")` (:54-65) — hashable the guided way.
  - `SendMessageRequest(_RequestModel)` (:145-156: `content: str = Field(min_length=1, max_length=65536)`,
    `state_id: UUID | None = None`); `_RequestModel.model_config = ConfigDict(extra="forbid")` (:68-71) —
    **not strict**, so the guided codec's strict check (guided_operations.py:26-27) would reject it as-is.
  - `_GuidedOperationRequest(BaseModel)` (:74-89) is the reusable strict base with
    `operation_id: str = Field(min_length=36, max_length=36)` and a canonical-UUID validator.
  - `recompose` (`routes/composer/compose.py:90-98`) takes **no request body** today; spec §2 (line 106)
    assumes "The freeform send and recompose DTOs gain a required `operation_id`" — a recompose DTO must be created.

---

## 9. SQLite vs PostgreSQL handling; SKIP LOCKED; CAS

- Dialect branch helpers: `_lower_sha256_check(..., dialect=)` (`GLOB` vs `~`), `_sql_non_blank_text` (`trim` vs `btrim`),
  `.ddl_if(dialect=...)` on every paired CHECK; triggers via `execute_if(dialect=...)`;
  `_guided_database_now` clock SQL; `_session_write_lock` RLock vs `pg_advisory_xact_lock`;
  `_DATABASE_CLOCK_SQL = {"postgresql": "SELECT clock_timestamp()", "sqlite": "SELECT CURRENT_TIMESTAMP"}`
  (`coordination/identity_authority.py:91-94`), reused by `websocket_ticket_authority.py:38-41`.
- `FOR UPDATE SKIP LOCKED`: `grep -rn "skip_locked\|SKIP LOCKED" src` → `coordination/websocket_ticket_authority.py:75`,
  `coordination/composer_progress_authority.py:200, 207, 221, 253`, `coordination/rate_limit_authority.py:65`
  (plus a comment in `core/landscape/journal.py:382`). None in `src/elspeth/web/sessions/` (`grep -rn skip_locked src/elspeth/web/sessions | wc -l` → 0).
  `composer_progress_authority.py:134-135` and `rate_limit_authority.py:49` **refuse non-PostgreSQL**;
  `websocket_ticket_authority` runs on both.
- Measured: SQLAlchemy silently drops the lock clause on SQLite —
  `select(sessions_table.c.id).with_for_update(skip_locked=True)` compiles to
  `SELECT sessions.id FROM sessions` under `sqlite.dialect()` and `SELECT sessions.id FROM sessions FOR UPDATE SKIP LOCKED`
  under `postgresql.dialect()` (control: plain `select` on SQLite gives the identical `SELECT sessions.id FROM sessions`).
  So a shared claim query is portable, but on SQLite the exclusion comes only from `BEGIN IMMEDIATE`
  (engine.py:116-137) plus a token-checked `UPDATE ... WHERE status='queued' AND claim_token IS NULL|expired`
  with `rowcount == 1` — the "transactional compare-and-swap" the spec names (line 154-155).
- CAS precedents with rowcount checks: guided takeover (service.py:5567-5589), renew (:5905-5919),
  bind (:3876-3911), terminal complete (:4033-4056); `archive_delete` rowcount (repository.py:5663-5665);
  composer progress uses `.returning(...)` (composer_progress_authority.py:233, 262).

---

## 10. PostgreSQL / SQLite schema tests to extend

- PG (serial, `-m testcontainer -n 0`): `tests/testcontainer/web/test_schema_probe_postgres.py` —
  `test_fresh_create_reaches_current` :108, `test_postgres_digest_checks_enforce_every_inventoried_shape` :194,
  guided status-bundle/diagnostics tests :260, :297, `test_postgres_guided_operation_takeover_fences_late_worker` :512,
  `test_postgres_concurrent_expired_reserve_has_one_takeover_winner` :649 (concurrency template for claim races),
  `test_postgres_session_audit_triggers_are_installed_and_enforced` :962 (exact trigger set),
  `test_postgres_guided_operation_locator_constraints_reject_invalid_bundles_and_cross_session_refs` :1150.
  Related: `test_session_operation_fence_postgres.py`, `test_guided_atomic_settlement_postgres.py`,
  `test_cross_process_composer_postgres.py`, `test_session_mutation_fencing_postgres.py`.
  `tests/testcontainer/web/conftest.py` shares one container and rejects xdist workers (AGENTS.md).
- SQLite: `tests/unit/web/sessions/test_schema.py` (epoch pin :368, coordination tables :369-385, PG DDL
  trigger list :585-609, stale-epoch rewrite tests :1050-1130), `tests/unit/web/sessions/test_models.py`
  (CHECK text pins, e.g. :270), `tests/integration/web/composer/guided/test_guided_operations_schema.py`,
  `tests/integration/web/composer/guided/test_schema9_epoch.py` (epoch pin + stale-epoch refusal :66).
