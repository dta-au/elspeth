# PostgreSQL gate: Landscape metadata reuse diagnosis

Read-only source/log analysis at `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`, in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. No tests, probes, scans, installs, provider calls, production/test edits, or git mutations were performed by this agent. This report is the only written artifact. AGENTS.md and CONTRIBUTING.md (including the whole-tree gate section) were consulted.

## Diagnosis and confidence

The strongest explanation is a **production Landscape schema-creation metadata reuse defect**, exercised by serial mixed-dialect tests: PostgreSQL `create_all` leaves SQLAlchemy `_create_rule` annotations on the module-global Landscape foreign-key constraints. Later SQLite creation silently omits those constraints. It is not necessary to posit collection-time test mutation, conditional-DDL pollution, or a PostgreSQL admission/locking regression.

This mechanism is established directly in installed SQLAlchemy source, the affected Landscape schema graph, and an existing Sessions fix documenting the identical issue. The exact failing full-run metadata objects have not been inspected live and the proposed minimal reproductions below were not executed here; therefore attribution of this particular run remains a source-supported diagnosis pending that final probe.

## Evidence

1. The full frozen-run trace fails at `test_blob_mutation_admission_postgres.py:272`, `rows = _durable_rows(url)`, after the waiter has actually blocked, the writer committed, and `admission.result(timeout=5) is not None` passed. Later audit payload and resource assertions are **not reached**, so those should not be reported as passing. The trace reports no foreign keys at all on `audit_export_snapshot_chunks`, `audit_export_snapshots`, `sink_effect_export_snapshots`, `sink_effect_members`, `sink_effect_streams`, and `sink_effects`. The authoritative validator correctly refuses that SQLite store.

2. `tests/unit/web/auth/test_audit.py:634–646` constructs `AuthAuditRecorder(..., create_tables=True)` and later reopens through `LandscapeDB.from_url`. Construction is lazy: `AuthAuditRecorder.start()` (`web/auth/audit.py:750–762`) creates the Landscape only when the first event is written. `record_quota_exceeded` (`:1761`) enters `_open_landscape`, which calls `start()`. Thus `_setup` only constructs the recorder; this test's quota-exceeded callback creates the SQLite audit DB during admission. The earlier module cases do not establish that this audit schema has successfully reopened.

3. Installed SQLAlchemy (`.venv/lib/python3.12/site-packages/sqlalchemy/sql/ddl.py`) has this complete mechanism:
   - `SchemaGenerator.visit_metadata` at `:956` calls `sort_tables_and_constraints`; its final `(None, fkcs)` branch traverses deferred FKs.
   - The sorter at `:1340–1448` detects cycles, removes **all removable FKs of a participating table**, and returns them for subsequent ALTER creation. This explains why even outward FKs to `runs`, `nodes`, `tokens`, and `rows` are absent, rather than only cycle-closing FKs.
   - `visit_foreign_key_constraint` at `:1046–1051` returns for dialects without ALTER support, but otherwise executes `AddConstraint(constraint)`.
   - `AddConstraint` at `:754–785` defaults `isolate_from_table=True`; its constructor assigns `element._create_rule = ..._create_rule_disable`. That callable unconditionally returns `False` (`:446–454`). There is no restore here.
   - SQLite's later `visit_table` resets `include_foreign_key_constraints=None` because it lacks ALTER support (`:1014–1017`), but the DDL compiler still requires `constraint._should_create_for_compiler(self)` (`sql/compiler.py:6859`). `sql/schema.py:4167` rejects a constraint when `_create_rule` returns false. The global annotation consequently suppresses SQLite inline FKs.

4. Landscape has precisely the relevant cycles (`core/landscape/schema.py:1686–2101`): snapshots ↔ chunks; effects ↔ members; effects ↔ export-snapshot bindings; effects ↔ streams. All six FK-empty tables in the trace participate. These constraints are ordinary unconditional FKs; the nearby `ddl_if` landscape declarations found by source search concern the unrelated run-start admission CHECK.

5. Production Landscape creation reuses global metadata without restoring SQLAlchemy state:
   - `core/landscape/database.py:1388`, `_create_tables`, reached by constructor `:1073`;
   - `database.py:1893`, `in_memory`;
   - `database.py:2088`, `from_url(create_tables=True)`;
   - **also** `web/schema_probe.py:351`, `init_landscape_schema`.
   These are ordinary runtime paths, not test mutation helpers. A source search for `AddConstraint|DropConstraint` in `src` and `tests` found no manual repository calls; SQLAlchemy's own schema generator is sufficient.

6. The repository already fixes this exact issue for Sessions: `web/sessions/schema.py:130–153`, `_create_session_tables`, documents the PostgreSQL→SQLite `_create_rule` leak, locks global metadata use, snapshots prior rules, calls `metadata.create_all`, and restores rules in `finally`. `tests/testcontainer/web/test_schema_probe_postgres.py:492` explicitly pins `test_postgres_session_init_does_not_poison_later_sqlite_schema`. There is no equivalent guard on the Landscape callers above.

7. The full testcontainer log records earlier successful runtime PostgreSQL Landscape creation well before the failing test: `test_aggregation_error_route_postgres.py::test_failed_batch_disposition_on_postgres[named-sink]` at log line 30 calls `LandscapeDB.from_url(postgres_url)` at source line 95. Subsequent release-continuation and audit-export tests also construct PostgreSQL Landscapes. The failing admission test is log line 504. This supplies a concrete preceding runtime trigger; it does not prove which creation first changed each object. The isolated admission module's `pg_fenced` fixture initializes **Sessions**, whose guard prevents that separate metadata graph from leaking. An isolated green module therefore fits this mechanism.

## Smallest discriminating probes for the test-capacity owner

Prefer a fresh-process, no-server mechanism probe first. Using the worktree's explicit `PYTHONPATH`, import the Landscape metadata and SQLAlchemy `create_mock_engine`, `inspect`, and SQLite `create_engine`. Record `_create_rule` for each FK; use a no-op executor with `create_mock_engine("postgresql://", executor)` and call `metadata.create_all(mock_engine)`; record which rules changed. Create fresh SQLite tables with that same metadata and inspect the six tables' FK lists. Compare against a separate fresh-process SQLite-only control. Predict: PostgreSQL mock creation annotates the cycle tables and the subsequent SQLite schema has the same all-FK-empty set as the failure. This is not executed here and must not be presented as measured evidence until run. It needs no PostgreSQL instance or full-suite capacity, but still mutates metadata in its private process, so do not execute it in an existing test interpreter.

For a production-path reproducer using existing tests, run **one explicitly ordered serial invocation** with these two node IDs:

```text
tests/testcontainer/web/test_schema_probe_postgres.py::test_landscape_database_construction_stamps_postgres_identity[from_url]
tests/testcontainer/web/test_blob_mutation_admission_postgres.py::test_postgres_admission_waiting_on_container_sees_committed_output_reservation
```

The first creates a PostgreSQL Landscape through the exact production factory; the second is the current failure. Use `-m testcontainer -n 0`, explicit worktree `PYTHONPATH`, and lane-private logs/exit code. This is more discriminating than repeating the entire full suite or bisecting unrelated test modules. The already-running isolated admission module serves as the absence-of-preceding-Landscape control. If the two-node probe differs from the mock prediction, instrument rule changes directly before/after production creation rather than widening to speculative collection pollution.

For a direct production regression test later, create a PostgreSQL Landscape, then create **and reopen** a SQLite Landscape in the same process, checking the full schema (or exact FK identities). Cover constructor/factory/init entry points and concurrent creation as required by the chosen repair. The Sessions guard is a concrete local implementation precedent. Do not loosen schema admission or special-case the test's SQLite store: the missing FKs are real integrity loss.

## Remaining limits

The full failing audit.db itself was not queried; evidence comes from the complete provided failure trace. No fresh test result is claimed here. Mixed PostgreSQL/SQLite use in one process is the demonstrated reachable condition from source; this analysis does not establish impact in a deployment that only initializes one dialect per process. The omission can exist during the first live SQLite connection because `from_url` validates before creation and does not run full shape validation again after `create_all`; reopening catches it. This makes the creation behavior a product defect worth fixing even though the broad test suite exposed it.
