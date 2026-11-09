# Landscape metadata reuse repair — design and implementation plan

> For agentic workers: use superpowers:subagent-driven-development with TDD and independent review. Parent coordinates integration and broad verification.

**Goal:** Creating a PostgreSQL Landscape must not make a later SQLite Landscape omit foreign keys in the same process.

**Architecture:** Apply the established Sessions metadata ownership pattern to Landscape: one shared lock protects snapshot, create_all and exact restoration in finally. All four production Landscape creation paths use that one helper. Keep schema definitions and preserve/refuse validation unchanged.

**Tech stack:** Existing Python, SQLAlchemy, SQLite and PostgreSQL; no dependency change.

**Spec:** User's authorized Composer remediation with every behavior pinned by tests; measured gate defect and source evidence in `d1-postgres-schema-triage.md`. This document records the coordinator's implementation choice, not a new product decision attributed to the user.

## Evidence and scope

Base application source is `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`. Full serial PostgreSQL gate: 1,068 passed, one failed, one skipped, healthy freeze. Isolated seven admission cases pass. Parent's two-node production sequence (`d1-dependency-pgsequence.*`) fails with exactly one pass/one failure: PostgreSQL Landscape construction followed by the admission quota-audit case reproduces the same missing SQLite FKs. `d1-dependency-postgres.*` is the isolated positive control.

SQLAlchemy's deferred PostgreSQL AddConstraint mutates constraint._create_rule on shared metadata. Six Landscape cycle tables subsequently have no SQLite foreign keys. The validator correctly rejects them on reopening; do not weaken it. Sessions already addresses this exact mechanism in `web/sessions/schema.py::_create_session_tables` and has a real PostgreSQL→SQLite regression test.

Approaches considered: (1) the existing locked snapshot/restore pattern, selected because it preserves table identity, DDL events and current initialization semantics; (2) cloning the entire metadata for each initialization, which would require proving all table/DDL/event identity semantics and is unnecessary for this focused repair. No generic framework or Sessions refactor is needed.

## Global constraints

- Preserve every foreign key, CHECK, identity stamp, index, trigger and current-schema admission rule.
- Existing unsupported databases remain unchanged and refused. No migration, repair-in-place, reset, suppression, signed-authority change or new DML authority.
- Restore the exact prior constraint rule objects, including non-None rules, on success and every exception; never replace all rules with None.
- One shared Landscape lock covers snapshot through restoration for every creation entry point; another initializer cannot observe/capture transient rules.
- Keep actual Engine/Connection forwarding and checkfirst semantics. Do not convert database errors to model-input refusals.
- No providers, dependency installs, subagents, commits, pushes or Page edits by implementer. Parent owns broad test capacity and integration.
- Work only in the parent-assigned isolated tree, with both source roots proved. Read AGENTS.md and CONTRIBUTING.md whole-tree gates before edits.

## Review focus

The tests below must cover: PostgreSQL→SQLite on every entry point; exact restoration on successful and failed DDL; concurrent initializers; non-None pre-existing rules; and preservation of stale-store refusal. Do not limit evidence to a mocked helper being called or to a missing-new-method red.

## One implementation task

Primary production files:
- `src/elspeth/core/landscape/database.py`: one private module helper and shared lock; route `_create_tables`, `in_memory`, and `from_url(create_tables=True)` through it.
- `src/elspeth/web/schema_probe.py`: route `init_landscape_schema` through the same helper. Its existing import of the Sessions private helper is a layering precedent; no Web dependency may be introduced into core.

Suggested interface: `_create_landscape_tables(bind: Engine | Connection, *, checkfirst: bool = True) -> None`. Use the project's existing concrete SQLAlchemy types and preserve exact private rule values without introducing duck-typed security dispatch. If a small core sibling module is clearly preferable, explain the reason before introducing it; the behavior and all four consumers are fixed requirements.

Test homes: existing `tests/testcontainer/web/test_schema_probe_postgres.py` for real mixed-dialect paths; existing Landscape database unit tests or a small focused unit module for failure/restoration/concurrency contracts. Read applicable scoped AGENTS files. The current admission test is an acceptance case, not the place to bypass schema validation.

- [ ] Add a meaningful regression using actual PostgreSQL Landscape creation followed by creation and reopening/full validation of a fresh SQLite Landscape in the same process. Parameterize PostgreSQL constructor, from_url and Web init entry points; include the in_memory SQLite path's full schema validation. Preserve existing single-dialect positive controls.
- [ ] Run the new regression red on current production source. Preserve complete command/output/exit. Parent's two-node failing sequence is corroborating evidence, not a substitute for new test assertions.
- [ ] Add the narrow shared lock/snapshot/try-finally/restore helper and route all four real calls through it. Preserve constructor/init errors, engine ownership and existing identity/index ordering.
- [ ] Pin exact restoration of original non-None rule identities on successful and failed DDL. Use actual SQLAlchemy behavior for mutation; an appropriately scoped injected DDL failure may establish exceptional cleanup. Do not fake the restoration itself.
- [ ] Pin concurrent creation with synchronization: one initializer paused after SQLAlchemy changes rules, a second cannot enter its snapshot/create interval early, then both complete (or one fails deliberately) with correct SQLite schema and original metadata restored. Avoid timing-only sleeps as the sole oracle.
- [ ] Run scoped green including all seven admission cases after a real PostgreSQL Landscape initializer in the same pytest process, the new mixed-dialect cases, relevant current/stale schema controls and focused database unit tests. Preserve exact FK checks and rejection behavior.
- [ ] Run scoped Ruff/mypy and relevant contract/source-binding gates for changed inputs; report required expensive checks to parent rather than launching a competing full suite. Do not blanket-refresh bindings. Any ordinary binding maintenance needs exact reviewed producer evidence and retained negative controls.
- [ ] Freeze the candidate, write `d1-landscape-metadata-report.md` and full `d1-landscape-metadata-review.diff` including new files in the canonical lane, then return for Astra spec/quality review.

## Coordination

Parent assigns the worktree. Other frozen trees hold D1 test/evidence repairs, independently approved by Astra; those eleven test paths will be integrated separately. Do not redo their known baseline failures or edit another agent's tree. Parent will advance this tree across that test-only commit when available, at a clean test boundary. Application source at14fe is otherwise the same.

One focused `-n 0` process at a time is allowed. Both `PYTHONPATH=<tree>/src:<tree>/elspeth-lints/src` and pytest `-o pythonpath="<tree>/src <tree>/elspeth-lints/src"` are mandatory. Use exact lane-private command/exit logs and freeze hashes. Another parent-owned source-analysis gate remains active; no full backend/PostgreSQL suite or all-rule scan from this task.

The repair reaches shared database initialization, so parent must reassess required whole-tree, backend and PostgreSQL verification on the combined candidate. A locally green reproducer does not establish that broader result.
