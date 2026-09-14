### Task I0: One schema pass: token ledger nullability

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: nothing (a workstream start). Runs before: I8. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Everything the identity workflow sprint needs in the sessions schema already
ships at epoch 56 (measured on HEAD 072141b75, 2026-09-13):
`review_attestations.author_identity_id` NOT NULL and
`ck_review_attestations_reviewer_is_not_author` (`models.py:3751-3774`),
`review_requests.reviewer_identity_id` nullable RESTRICT FK (`:3722`),
`approvals.requested_by_identity_id` / `approver_identity_id` and
`ck_approvals_author_is_not_approver` (`:3609-3649`), `approval_decisions`
(`:3668`), `library_entries.published_by_identity_id` / `curated_by_identity_id`
and `ck_library_entries_curator_is_not_publisher` (`:3798-3822`),
`quota_policies.identity_id` nullable FK with both partial unique indexes
(`:3836-3881`), `token_usage_ledger.identity_id` / `session_id` nullable
RESTRICT FKs (`:3897-3910`), and `durable_history_exists` — a local at
`src/elspeth/web/coordination/repository.py:731`, not a function — already ORs
`approvals`, `review_requests`, `review_attestations` and `library_entries`
(`:761-771`). None of that is touched here.

The one schema change the sprint still needs is that
`token_usage_ledger.prompt_tokens` and `completion_tokens` are `nullable=False`
(`models.py:3914-3915`). The spec row `docs/specs/2026-09-02-pluggable-sso-design.md:1422`
lists both columns without a NULL marker yet requires the adapter to be
"preserving unknown usage" and rules "An empty ledger is never measured zero";
the Landscape `calls` twin at `:849-856` is explicit: "Missing measures remain
NULL; reported zero remains zero." **Resolution adopted by this task: NULL means
unknown usage, never zero.** A provider that reports no usage leaves both
measures NULL, and Task I1's `daily_token_total` returns `None` when any row of
the day is NULL so R14 keeps refusing with `token_accounting_unavailable` rather
than admitting on a fabricated zero.

An earlier decision counted "exactly two changes" (the two columns and the epoch). The
measured count is three source edits: `src/elspeth/web/sessions/schema.py:36`
holds `_COORDINATION_HARD_CUT_EPOCH = 56`, compared to `SESSION_SCHEMA_EPOCH` by
exact equality at `schema.py:531` inside `_validate_coordination_hard_cut_metadata`,
which `initialize_session_schema` reaches on every fresh database
(`schema.py:167` → `_validate_current_schema:489`). Both prior bumps touched
that file (`976be9ecb` 54→55, `1cee88f30` 55→56). Bumping `models.py:333`
alone refuses to open every session database on every dialect; Step 5 proves
that before Step 6 moves the sentinel.

Ticket `elspeth-ff89d2bea0` (Landscape `run_web_plugin_policy` quota-policy ids
and secret-wiring hash) is **closed**, integrated at `release/0.8.1@b0662fa5f`;
Landscape is at epoch 40 on HEAD with the four nullable `calls` token columns
(`core/landscape/schema.py:2130-2136`). This task makes NO Landscape change.

**Files:**
- Modify: `src/elspeth/web/sessions/models.py:331-333` (epoch history and `SESSION_SCHEMA_EPOCH = 56`) and `:3914-3915` (the two `nullable=False` ledger columns)
- Modify: `src/elspeth/web/sessions/schema.py:35-36` (`_COORDINATION_HARD_CUT_EPOCH = 56` and its comment)
- Test: `tests/unit/web/sessions/test_workflow_governance_schema.py` (two new tests appended after `test_token_usage_source_is_closed`, `:339-350`; uses that file's `engine` fixture `:42-51`, `NOW` `:38`, and its existing imports `uuid`, `insert`, `inspect`, `select`, `token_usage_ledger_table`)
- Modify (test pins on the literal 56): `tests/unit/web/sessions/test_schema.py:282-283`, `tests/integration/web/composer/guided/test_schema9_epoch.py:52-54`, `tests/unit/contracts/test_web_blob_fencing.py:3188-3196`, `tests/unit/web/sessions/test_interpretation_events_table.py:228-229`, `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py:61-64`, `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py:17-18`
- Modify (doc pins on the literal 56, each read by a test): `CHANGELOG.md:9-12,22` (`tests/unit/website/test_release_site_contract.py:61`, `tests/unit/docs/test_staging_session_recreation_policy.py:47`), `README.md:167-168` (`tests/unit/docs/test_readme_release_surface.py:35`), `docs/guides/sharing-pipelines.md:73,99-100` (`tests/unit/docs/test_release_version_surfaces.py:125`), `docs/runbooks/staging-session-db-recreation.md:5,7,13,157,159,735` (`test_staging_session_recreation_policy.py:33-38`), `docs/runbooks/aws-ecs-deployment.md:1609,1611` (`test_release_version_surfaces.py:134` compares the Scenario B record, including the `structural_changes` label, to `_acceptance_common/schema_facts.py:26-30`) and `:1637,1642` (already stale at "55" on HEAD; not test-pinned; corrected here), `docs/runbooks/azure-container-apps-cold-install.md:78` and `docs/runbooks/azure-container-apps-deployment.md:109,418,431,505` (`tests/unit/web/test_azure_container_apps_runbook_contract.py:182-197`)
- No edit: `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py`, `src/elspeth/web/_acceptance_common/schema_facts.py`, `tests/unit/web/aws_ecs_acceptance/`, `tests/testcontainer/web/test_schema_probe_postgres.py:147` and `test_external_deployment_postgres.py:555` all derive from the constant (the ticket's listing of `receipt_contracts.py` is stale). `docs/reference/configuration.md:2191` ("Sessions epoch 55") records where a feature landed, not the current epoch.

**Interfaces:**
- Consumes: nothing from an earlier task (I0 is the root of Workstream I). HEAD fixture `engine` at `tests/unit/web/sessions/test_workflow_governance_schema.py:42-51`: `create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)`, then `initialize_session_schema(eng)`, then `ensure_test_identity(conn, identity_id="alice")` inside `eng.begin()`.
- Produces: `token_usage_ledger_table.c.prompt_tokens` and `token_usage_ledger_table.c.completion_tokens` (`src/elspeth/web/sessions/models.py:3914-3915`) are `Integer, nullable=True`; NULL means unknown usage, never zero. Task I1's `RepositoryQuotaAuthority.record_token_usage` (signature defined in I1) may write `None` to both, and its `daily_token_total` returns `None` when any row of the day carries a NULL measure. `SESSION_SCHEMA_EPOCH == 57` (`models.py:333`) and `_COORDINATION_HARD_CUT_EPOCH == 57` (`schema.py:36`); Landscape `SQLITE_SCHEMA_EPOCH` stays 40. No later task in Workstream I adds a column, a CHECK, a NOT NULL constraint, or bumps either epoch.

- [ ] **Step 1: Write the two failing tests.** Append to `tests/unit/web/sessions/test_workflow_governance_schema.py` directly after `test_token_usage_source_is_closed` (ends at `:350`, before `class TestArchiveKeepsGovernanceHistory` at `:353`). No new imports are needed: `uuid`, `insert`, `inspect`, `select`, `token_usage_ledger_table` and `NOW` are already bound at `:13-38`.

```python
def test_token_usage_ledger_admits_unknown_usage_as_null_never_zero(engine) -> None:
    """A provider that reported no usage leaves both measures NULL.

    Spec ``docs/specs/2026-09-02-pluggable-sso-design.md:849-856`` rules the
    Landscape ``calls`` twin: "Missing measures remain NULL; reported zero
    remains zero." The ledger row the boot probe writes (``identity_id`` NULL,
    ``source='system'``, ``models.py:3898``) is exactly such a row, and I1's
    daily aggregate must be able to see it as unknown rather than as 0.
    """
    entry_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id=entry_id,
                identity_id=None,
                source="system",
                session_id=None,
                run_id=None,
                model="gpt",
                prompt_tokens=None,
                completion_tokens=None,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                recorded_at=NOW,
            )
        )
    with engine.connect() as conn:
        row = conn.execute(
            select(
                token_usage_ledger_table.c.prompt_tokens,
                token_usage_ledger_table.c.completion_tokens,
            ).where(token_usage_ledger_table.c.entry_id == entry_id)
        ).one()
    assert row.prompt_tokens is None
    assert row.completion_tokens is None


def test_token_usage_ledger_nullability_moved_on_exactly_the_two_measure_columns(engine) -> None:
    """Epoch 57 relaxed two columns and nothing else on the table."""
    nullable = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("token_usage_ledger")}
    assert nullable["prompt_tokens"] is True
    assert nullable["completion_tokens"] is True
    # Negative control: the columns that were NOT NULL at epoch 56 still are,
    # so this instrument goes red if nullability spreads past the two measures.
    assert nullable["source"] is False
    assert nullable["model"] is False
    assert nullable["recorded_at"] is False
    # The two measures that were already nullable at epoch 55 stay nullable.
    assert nullable["cached_prompt_tokens"] is True
    assert nullable["reasoning_tokens"] is True
```

- [ ] **Step 2: Run the two tests to verify they fail on HEAD.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_workflow_governance_schema.py -k "unknown_usage_as_null or nullability_moved" -n 0 > /tmp/i-lane-i0-step2.log 2>&1; echo exit=$?
```

Expected: `exit=1`, two failures. `test_token_usage_ledger_admits_unknown_usage_as_null_never_zero` fails with
`sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) NOT NULL constraint failed: token_usage_ledger.prompt_tokens`;
`test_token_usage_ledger_nullability_moved_on_exactly_the_two_measure_columns` fails at its first assertion with
`assert False is True` (`nullable["prompt_tokens"]`).

- [ ] **Step 3: Relax the two ledger columns.** In `src/elspeth/web/sessions/models.py` replace lines 3914-3915, which read

```python
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
```

with

```python
    # NULL means the provider reported no usage for this call: unknown, never
    # zero. A daily quota aggregate over any NULL row is itself unknown, so
    # R14 keeps refusing with token_accounting_unavailable instead of admitting
    # on a fabricated 0 (spec sso-design.md:849-856, the Landscape ``calls``
    # twin: "Missing measures remain NULL; reported zero remains zero").
    Column("prompt_tokens", Integer, nullable=True),
    Column("completion_tokens", Integer, nullable=True),
```

Do not add a CHECK constraint, an index, or any other column: the ledger's other columns, its `ck_token_usage_ledger_source` CHECK and its `ix_token_usage_ledger_identity_recorded` index are already on HEAD (`models.py:3893-3927`), so the schema change is limited to these two columns.

- [ ] **Step 4: Bump `SESSION_SCHEMA_EPOCH` with a numbered history entry.** In `src/elspeth/web/sessions/models.py` the last history line (`:332`) is the unnumbered `# Coupled cut: sparse proposal display and structured stored validation errors.` followed by `SESSION_SCHEMA_EPOCH = 56` (`:333`). Insert the epoch-57 entry between them in the `#   NN -> ` form the earlier entries use (`:299`, `:312`, `:314`), and change the constant:

```python
# Coupled cut: sparse proposal display and structured stored validation errors.
#   57 -> token_usage_ledger.prompt_tokens and completion_tokens become
#        nullable (identity workflow sprint, Task I0): a provider that reports
#        no usage leaves both NULL, meaning unknown, never zero, so a daily
#        quota aggregate over a NULL row is itself unknown and R14 keeps
#        refusing with token_accounting_unavailable rather than admitting on
#        a fabricated zero. Every other workflow column, CHECK and partial
#        index the sprint needs shipped at epochs 52 and 55. Pre-1.0
#        delete-and-recreate boundary; no migration, rollback_permitted:
#        false (sessions.db only; auth.db is untouched). Landscape stays at
#        epoch 40; this is a sessions-only cutover.
SESSION_SCHEMA_EPOCH = 57
```

- [ ] **Step 5: Run once to prove the coordination hard-cut sentinel is coupled.** With `schema.py:36` still at 56:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_workflow_governance_schema.py -k "unknown_usage_as_null" -n 0 > /tmp/i-lane-i0-step5.log 2>&1; echo exit=$?
```

Expected: `exit=1`; the `engine` fixture errors inside `initialize_session_schema` with
`elspeth.web.sessions.schema.SessionSchemaError: Session database schema does not match SESSION_SCHEMA_EPOCH=57. Delete the old session database and restart; pre-release ELSPETH does not migrate web session databases. Detail: coordination schema epoch mismatch. Expected: 56. Actual: 57.`
This is the failure an operator would see on every session database if the sentinel were left behind; it is why Step 6 is not optional.

- [ ] **Step 6: Move the coordination hard-cut sentinel.** In `src/elspeth/web/sessions/schema.py` replace lines 35-36, which read

```python
# Coupled cut: sparse proposal display and structured stored validation errors.
_COORDINATION_HARD_CUT_EPOCH = 56
```

with

```python
# Epoch 57: token_usage_ledger prompt/completion measures nullable (unknown,
# never zero). Tracks SESSION_SCHEMA_EPOCH by exact equality; see
# ``_validate_coordination_hard_cut_metadata`` below.
_COORDINATION_HARD_CUT_EPOCH = 57
```

- [ ] **Step 7: Run the two new tests to verify they pass.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_workflow_governance_schema.py -k "unknown_usage_as_null or nullability_moved" -n 0 > /tmp/i-lane-i0-step7.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `2 passed`.

- [ ] **Step 8: Update the six test files that pin the literal 56.** Each edit changes the assertion and extends the file's own epoch commentary; preserve everything else.

`tests/unit/web/sessions/test_schema.py:282-283` — replace

```python
    # 56 couples sparse proposal display with structured stored validation errors.
    assert SESSION_SCHEMA_EPOCH == 56
```

with

```python
    # 56 couples sparse proposal display with structured stored validation errors.
    # 57 makes the token ledger's prompt/completion measures nullable
    # (unknown usage is NULL, never zero); Landscape stays at 40.
    assert SESSION_SCHEMA_EPOCH == 57
```

`tests/integration/web/composer/guided/test_schema9_epoch.py:52-54` — replace

```python
    # Session epoch 56 adds sparse proposal arguments and structured validation
    # errors. These changes deploy together as the pair (56, 40).
    assert SESSION_SCHEMA_EPOCH == 56
```

with

```python
    # Session epoch 56 adds sparse proposal arguments and structured validation
    # errors. Session epoch 57 makes the token ledger's prompt/completion
    # measures nullable. These changes deploy together as the pair (57, 40).
    assert SESSION_SCHEMA_EPOCH == 57
```

`tests/unit/contracts/test_web_blob_fencing.py:3188-3196` — in the docstring replace `then 56 for sparse proposal arguments\n    and structured validation errors. The claim the test` with `then 56 for sparse proposal arguments\n    and structured validation errors, then 57 for the nullable token-ledger\n    usage measures. The claim the test`, and replace `assert SESSION_SCHEMA_EPOCH == 56` (`:3196`) with `assert SESSION_SCHEMA_EPOCH == 57`.

`tests/unit/web/sessions/test_interpretation_events_table.py:228-229` — replace

```python
    # 56: sparse proposal arguments and structured validation errors.
    assert SESSION_SCHEMA_EPOCH == 56
```

with

```python
    # 56: sparse proposal arguments and structured validation errors.
    # 57: nullable token-ledger prompt/completion measures (unknown, never zero).
    assert SESSION_SCHEMA_EPOCH == 57
```

`tests/unit/web/sessions/test_blob_inline_resolutions_schema.py:61-64` — replace

```python
    # 56: sparse proposal arguments and structured validation errors.
    assert SESSION_SCHEMA_EPOCH == 56
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA user_version")).scalar_one() == 56
```

with

```python
    # 56: sparse proposal arguments and structured validation errors.
    # 57: nullable token-ledger prompt/completion measures (unknown, never zero).
    assert SESSION_SCHEMA_EPOCH == 57
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA user_version")).scalar_one() == 57
```

`tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py:17-18` — replace

```python
    # Paired with Landscape40: identity ownership and admission evidence.
    assert SESSION_SCHEMA_EPOCH == 56
```

with

```python
    # Paired with Landscape40: identity ownership and admission evidence.
    # 57: nullable token-ledger prompt/completion measures (unknown, never zero).
    assert SESSION_SCHEMA_EPOCH == 57
```

- [ ] **Step 9: Update every document that carries the literal.** Change only the digit and append the one-clause reason where the sentence enumerates what the epoch carries; keep every existing line wrap, because the site tests match the wrapped text exactly (`test_release_site_contract.py:61` matches `advances from 53\nto {SESSION_SCHEMA_EPOCH}`, and `test_readme_release_surface.py:33` whitespace-normalises before matching). Every line number below is HEAD's: within each file apply the edits from the highest line number downward (the two-line insert after `staging-session-db-recreation.md:13` and the extra CHANGELOG line would otherwise shift the later cites).

`CHANGELOG.md:9-12` (under `## 0.8.1 - 2026-09-10`; confirm the section with the operator before the first commit, see Global Constraints) — replace

```markdown
**Breaking pre-1.0 schema cutover:** `SESSION_SCHEMA_EPOCH` advances from 53
to 56 for durable Composer progress, request lifecycle leases, identity owner
foreign keys, approval revocation provenance, run admission decisions, sparse
proposal arguments and structured validation errors.
```

with

```markdown
**Breaking pre-1.0 schema cutover:** `SESSION_SCHEMA_EPOCH` advances from 53
to 57 for durable Composer progress, request lifecycle leases, identity owner
foreign keys, approval revocation provenance, run admission decisions, sparse
proposal arguments, structured validation errors and nullable token-ledger
usage measures (unknown usage is NULL, never zero).
```

`CHANGELOG.md:22` — `epoch 56 and Landscape databases below epoch 40 must be recreated together.` becomes `epoch 57 and Landscape databases below epoch 40 must be recreated together.`

`README.md:168` — `to 56 and Landscape epoch 38 to 40; guided schema remains at 11. Archive or` becomes `to 57 and Landscape epoch 38 to 40; guided schema remains at 11. Archive or`. Line 167 (`**Operational:** 0.8.1 is a pre-1.0 database cutover from session epoch 53`) is unchanged; keep the line break after `53`.

`docs/guides/sharing-pipelines.md:73` — `contract. The release expects `SESSION_SCHEMA_EPOCH=56` and` becomes `contract. The release expects `SESSION_SCHEMA_EPOCH=57` and`.

`docs/guides/sharing-pipelines.md:99-100` — replace

```markdown
approval revocation provenance and durable admission decisions. Session epoch 56
preserves sparse proposal arguments and structured validation errors. Landscape epoch
```

with

```markdown
approval revocation provenance and durable admission decisions. Session epoch 56
preserves sparse proposal arguments and structured validation errors. Session
epoch 57 makes the token ledger's prompt and completion measures nullable, so
unknown usage is recorded as NULL and never as zero. Landscape epoch
```

`docs/runbooks/staging-session-db-recreation.md`:
- `:5` — `## Current Cutover: 0.8.1 replica recovery and identity admission (session epoch 56 and Landscape epoch 40)` becomes `## Current Cutover: 0.8.1 replica recovery and identity admission (session epoch 57 and Landscape epoch 40)`.
- `:7` — `0.8.1 advances `SESSION_SCHEMA_EPOCH` from 53 to 56 and Landscape` becomes `0.8.1 advances `SESSION_SCHEMA_EPOCH` from 53 to 57 and Landscape`.
- `:13` — `epoch 56 preserves sparse proposal arguments and structured validation errors.` becomes `epoch 56 preserves sparse proposal arguments and structured validation errors.\nSession epoch 57 makes the token ledger's prompt and completion measures\nnullable (unknown usage is NULL, never zero).` (two new lines inserted after `:13`).
- `:157` — `unsupported: keep the service drained, repair the epoch-56 release forward,` becomes `unsupported: keep the service drained, repair the epoch-57 release forward,`.
- `:159` — `session-epoch-56/Landscape-epoch-40 record when binding candidate and rollback` becomes `session-epoch-57/Landscape-epoch-40 record when binding candidate and rollback`.
- `:735` — `sqlite3 "$DB_PATH" 'PRAGMA user_version;'         # expect 56 (== SESSION_SCHEMA_EPOCH)` becomes `sqlite3 "$DB_PATH" 'PRAGMA user_version;'         # expect 57 (== SESSION_SCHEMA_EPOCH)` (keep the column alignment; `test_staging_session_recreation_policy.py:20-24` matches this line by regex).

`docs/runbooks/aws-ecs-deployment.md`:
- `:1609` — `    "candidate": {"session_epoch": 56, "landscape_epoch": 40, "run_web_plugin_policy_present": true},` becomes `    "candidate": {"session_epoch": 57, "landscape_epoch": 40, "run_web_plugin_policy_present": true},`.
- `:1611` — `"structural_changes": "session_epoch_35_to_56_landscape_epoch_29_to_40_blob_cleanup_guided_decline_row_union_barrier_and_coordination_schema",` becomes `"structural_changes": "session_epoch_35_to_57_landscape_epoch_29_to_40_blob_cleanup_guided_decline_row_union_barrier_and_coordination_schema",` (this label is compared verbatim against `schema_facts._SCENARIO_B_STRUCTURAL_CHANGES`).
- `:1637` — `session epoch 55, Landscape epoch 40 and `run_web_plugin_policy` presence,` becomes `session epoch 57, Landscape epoch 40 and `run_web_plugin_policy` presence,` (was already stale on HEAD; not test-pinned).
- `:1642` — `not epoch 55. Pre-1.0 candidates do not migrate predecessor schemas: the old` becomes `not epoch 57. Pre-1.0 candidates do not migrate predecessor schemas: the old` (was already stale on HEAD; not test-pinned).

`docs/runbooks/azure-container-apps-cold-install.md:78` — `  session epoch 56 and Landscape epoch 40 initialized;` becomes `  session epoch 57 and Landscape epoch 40 initialized;`.

`docs/runbooks/azure-container-apps-deployment.md`:
- `:109` — `- The epoch-56 image (session epoch 56, Landscape epoch 40) in the registry.` becomes `- The epoch-57 image (session epoch 57, Landscape epoch 40) in the registry.`
- `:418` — `  with the schema-owner URLs and initializes both schemas at session epoch 56` becomes `  with the schema-owner URLs and initializes both schemas at session epoch 57`.
- `:431` — `> require both schema checks to pass after initialization at session epoch 56` becomes `> require both schema checks to pass after initialization at session epoch 57`.
- `:505` — `    "candidate": {"session_epoch": 56, "landscape_epoch": 40, "run_web_plugin_policy_present": true},` becomes `    "candidate": {"session_epoch": 57, "landscape_epoch": 40, "run_web_plugin_policy_present": true},`.

- [ ] **Step 10: Run every suite that pins the epoch or the ledger.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions tests/unit/contracts/test_web_blob_fencing.py tests/integration/web/composer/guided/test_schema9_epoch.py tests/unit/website tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/docs/test_readme_release_surface.py tests/unit/web/test_azure_container_apps_runbook_contract.py tests/unit/web/test_aws_ecs_runbook_contract.py tests/unit/web/aws_ecs_acceptance tests/unit/architecture/test_session_db_mutation_authority.py -n 0 > /tmp/i-lane-i0-unit.log 2>&1; echo exit=$?
```

Expected: `exit=0`. If `test_every_epoch_literal_matches_the_live_constants` (`test_azure_container_apps_runbook_contract.py:182`) fails, its assertion message names the exact `session epoch NN` literal still at 56; fix that line rather than the test.

- [ ] **Step 11: Run the PostgreSQL schema suites (schema change: the testcontainer run is mandatory).** Needs Docker.

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_schema_probe_postgres.py tests/testcontainer/web/test_external_deployment_postgres.py -m testcontainer -n 0 > /tmp/i-lane-i0-pg.log 2>&1; echo exit=$?
```

Expected: `exit=0`. `test_postgres_fresh_schema_stamps_cross_dialect_identity` (`test_schema_probe_postgres.py:143`) reads `SESSION_SCHEMA_EPOCH` and proves the fresh PostgreSQL schema reflects identically to the declared metadata, which is where a nullability change that SQLite tolerates but PostgreSQL reflects differently would surface; `test_external_deployment_postgres.py:555` stamps `SESSION_SCHEMA_EPOCH - 1` and expects `STALE`.

- [ ] **Step 12: Commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit && git commit -m "feat(sessions): token ledger admits unknown usage as NULL; SESSION_SCHEMA_EPOCH 56 -> 57" -- src/elspeth/web/sessions/models.py src/elspeth/web/sessions/schema.py tests/unit/web/sessions/test_workflow_governance_schema.py tests/unit/web/sessions/test_schema.py tests/integration/web/composer/guided/test_schema9_epoch.py tests/unit/contracts/test_web_blob_fencing.py tests/unit/web/sessions/test_interpretation_events_table.py tests/unit/web/sessions/test_blob_inline_resolutions_schema.py tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py CHANGELOG.md README.md docs/guides/sharing-pipelines.md docs/runbooks/staging-session-db-recreation.md docs/runbooks/aws-ecs-deployment.md docs/runbooks/azure-container-apps-cold-install.md docs/runbooks/azure-container-apps-deployment.md
```

Check `git show --stat HEAD` lists exactly those 16 files.
