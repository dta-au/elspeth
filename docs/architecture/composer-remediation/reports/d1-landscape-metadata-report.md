# Landscape metadata reuse repair

Candidate: `/workspace/elspeth/.claude/worktrees/composer-remediation-landscape-metadata`, based on `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`. No commit or staging was performed. The original/fix1 worktrees remain untouched. This implements the canonical `d1-landscape-metadata-brief.md` using the approved Sessions-style lock/snapshot/restore design.

## Result and review package

The confirmed PostgreSQL→SQLite metadata defect is repaired in this candidate. A single shared core helper serializes metadata creation and restores every constraint's exact prior `_create_rule` object in `finally`. All four production Landscape creation paths use it: constructor `_create_tables`, `in_memory`, `from_url(create_tables=True)`, and Web `init_landscape_schema`.

Final focused verification: **190 passed in 65.91 seconds**, process exit **0**, freeze exit **0**. Complete before/after manifests are equal across **6,205** tracked/new-file inputs, including symlink targets. Both source imports resolved inside this candidate tree. This is scoped evidence, not a full backend/PostgreSQL/whole-tree gate claim.

Full review diff, including the new unit module: `d1-landscape-metadata-review.diff`, **21,965 bytes**, SHA-256 `6ce81d77ca5d8f06e01fab3294444b7c470d5fab9c1cbe17a5e1057e239b63b4`.

Six changed paths:

- `src/elspeth/core/landscape/database.py`
- `src/elspeth/web/schema_probe.py`
- `tests/testcontainer/web/test_schema_probe_postgres.py`
- `tests/unit/core/landscape/test_database_metadata_creation.py` (new)
- `tests/unit/architecture/test_session_db_mutation_authority.py`
- `tests/unit/architecture/test_web_landscape_mutation_fencing.py`

These do not overlap the eleven paths in `d1-gate-repair-fix1-review.diff`. That D1 test-only repair is not integrated into this candidate.

## Production behavior and preserved contracts

`_create_landscape_tables(bind: Engine | Connection, *, checkfirst: bool = True)` holds `_LANDSCAPE_METADATA_CREATE_LOCK` from the original-rule snapshot through creation and restoration. It forwards the same Engine/Connection and `checkfirst`, does not clone or replace tables, and lets original DDL exceptions propagate. Exact restoration preserves pre-existing non-None rule identities, rather than clearing rules to None. Web imports the core helper; core acquires no Web dependency.

Schema definitions, constraints, indexes, triggers, epochs, identity stamps, validation, create/refuse boundaries and ordering around initialization remain unchanged. The helper removes SQLAlchemy's transient PostgreSQL DDL side effect; it does not repair existing databases. Current-store validation and stale-store preservation/refusal remain exercised by the focused controls.

The Web module's old `landscape_metadata` import became unused after delegation. The existing initializer-concurrency test now obtains metadata directly from its owning schema module, preserving its real `create_all` interception. No production test seam was added.

## TDD and runtime evidence

- `d1-landscape-metadata-red.log/.exit`: **13 failed**, exit **1**, before production edits. Nine actual PostgreSQL→SQLite combinations reproduced missing FKs on the same six cycle tables. Four unit cases failed on lost exact rule identities or a second DDL interval entering before restoration. They did not fail because a new method was missing. Four-file before/after SHA-256 lists matched for this red run.
- `d1-landscape-metadata-green.log/.exit`: the same **13 passed**, exit **0**, after the single production fix.
- `d1-landscape-metadata-scoped.log/.exit`: **173 passed**, exit **0**. Includes actual PostgreSQL construction before the entire seven-case admission module, mixed-dialect regressions, current/stale PostgreSQL controls, initializer serialization and focused database unit modules.
- `d1-landscape-metadata-final.log/.exit`: **190 passed**, exit **0**, adding the narrow source-binding and inventory controls to the above selection. `d1-landscape-metadata-final.command.json` records the complete exact argv/cwd/PYTHONPATH; `run-d1-landscape-metadata-final.py` is its reproducible driver. `d1-landscape-metadata-final.before.json/.after.json` are complete manifests; `.frozen-exit` is **0**. This corrected capture supersedes the earlier scoped hash attempt, whose preserved output skipped directory symlinks and must not support a complete-tree freeze claim.
- `d1-landscape-metadata-scoped.provenance` records `elspeth.__file__` and `elspeth_lints.__file__` inside the assigned tree. Every pytest process used `-n 0`, both explicit source roots in `PYTHONPATH` and `-o pythonpath`, and a lane-private log. Only one pytest process ran at once.

The initial red and green command was:

```text
PYTHONPATH=<candidate>/src:<candidate>/elspeth-lints/src <candidate>/.venv/bin/python -m pytest -n 0 -m 'testcontainer or not testcontainer' -o 'pythonpath=<candidate>/src <candidate>/elspeth-lints/src' tests/unit/core/landscape/test_database_metadata_creation.py tests/testcontainer/web/test_schema_probe_postgres.py::test_postgres_landscape_creation_preserves_later_sqlite_schema
```

The real mixed-dialect regression crosses PostgreSQL constructor/factory/Web init with SQLite constructor/factory/in-memory creation. It runs full schema validation and current classification; file stores are closed and reopened through normal admission. Its `finally` cleanup only isolates failed tests; success assertions run before cleanup. Unit tests generate the actual SQLAlchemy PostgreSQL deferred-constraint mutations with a mock engine, retain one real pre-existing non-None rule, verify original exception identity, and verify exact restoration on success/failure. Concurrency uses events plus a real observed lock: the second acquisition's blocked notification is the oracle, not an arbitrary sleep; both success and exceptional predecessors are covered, with current SQLite schemas and restored original metadata afterward.

## Source-binding maintenance and evidence

Only the two affected `_NEW_LANDSCAPE_STAMP_SOURCES` digests were changed. The core database source was already part of this complete-source proof, so the new helper is included in the dependency boundary, not merely the caller's digest. New mutation controls remove its lock or replace exact restoration with `None`; both must be refused. Existing locked-target, EMPTY-only admission, target identity, scope and source-completeness controls remain intact.

`d1-landscape-metadata-binding-red.log/.exit` recorded **13 failed**, exit **1**, at the original positive authority assertion with stale digests. After reviewed maintenance, `d1-landscape-metadata-binding-green.log/.exit` recorded the original **13 passed**; the final run also passes the two added helper mutations.

The reviewed producer diff leaves the stamp and its target unchanged. Source hashes are:

| Producer | Base | Candidate |
| --- | --- | --- |
| `core/landscape/database.py` | `b8da4ed519bbe887140a0a72f040ce264fc4910724c56ca0f55065c2a89653cf` | `099a5bff4fa162b87119cadbd5fc10109938cff4d6c4d2a1e8ac2e5d3f31f1ed` |
| `web/schema_probe.py` | `c0f27e271e6128ea96a4cb345f32ef4c61bbcdd338aba0a9eaaf1db34447851d` | `0e4ba641686cba00c9b459e3cc241ce686847211197c2563a4991f5a5d31c4cd` |

`d1-landscape-metadata-identities.json` is the actual two-file production-writer scanner output against the hash-verified base and candidate. `d1-landscape-metadata-identity-verification.txt` verifies **17 sites before/after**, **15** database source-line shifts of exactly +20 and one changed body fingerprint: `init_landscape_schema.body`, `73db976a22a18557` → `b887ed3ea6657001`. All other fields, multiplicities, domains and escape flags are identical. The **15** existing reviewed identities match after exact field maintenance; the two previously unresolved sites (`LandscapeDB._set_sqlite_schema_epoch`, `_run_locked`) remain unresolved. The positive identity comparison passes and a moved-line negative control is refused. No writer grant, scanner algorithm, schema-admission policy or signed authority was changed.

## Static checks, limitations and handoff

Scoped Ruff and format checks pass for all six changed paths (`d1-landscape-metadata-ruff.*`, `d1-landscape-metadata-format.*`, both exit **0**). `git diff --check` passes (`d1-landscape-metadata-diff-check.*`, exit **0**). Scoped mypy on both production files passes (`d1-landscape-metadata-mypy.*`, exit **0**, no issues in two source files); its existing unused-config-section note is retained in the log. Production bytes have not changed since that type check.

No full suite, all-rule scan, install, dependency update, provider call, subagent, commit, push or Page edit was performed. Parent owns Astra review, integration with the eleven D1 test repairs, and reassessment/running of required broad backend, PostgreSQL and source-authority gates on the combined candidate. Existing broad authority evidence from the other frozen tree cannot certify these new production bytes. No deferred implementation item or alternative design ruling was taken beyond the brief.
