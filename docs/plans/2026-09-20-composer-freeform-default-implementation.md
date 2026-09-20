# Composer Freeform default implementation

Implemented in `fc0bd33146326fca31f4d928f1674af1dce5fe4e`, based on `release/0.8.1` at `9dae0d50d7b7c9bb8085998f4cc1f725a8e8464d`. Merged locally into `release/0.8.1` at `9da7ffe340342b3d572f26abf1964c9c2f5b489e`. This is implementation and local integration evidence, not deployment acceptance.

## Behaviour delivered

- No-row preferences reads, first unrelated preference writes, and the actual database column default all select Freeform. Explicit Guided preferences remain supported.
- Tutorial completion, skip, and early exit persist Freeform with explicit completion intent. A completed tutorial opens the existing pipeline after authoritative Guided exit, preserving its context. Skip opens a fresh Freeform session.
- Departure waits for authoritative state; pending transitions and failed saves remain retryable. Startup/skip races, stale session selection, and same-session Guided re-entry cannot publish a false successful departure.
- Progress and completion writes serialize in the frontend. An atomic database predicate rejects late populated progress with HTTP 409 even when a PostgreSQL writer read stale prior state. Completion/reset requests cannot carry contradictory populated resume fields.
- Guided is behind the secondary Composer options disclosure. Preferences list Freeform first and focus the selected choice. Guided retains its existing goal collection, confirmation, and visible Freeform exit.
- Removed the obsolete default-change banner, its persistence/wire/store contract and cross-tab branch, and its unused workspace slot. The separate Freeform introduction remains supported. No compatibility aliases, migrations, feature flags, or suppressed findings were added.

## Measured validation

The operator narrowed final validation to relevant unit and integration tests. Every result below came from a completed process with exit 0 and both Python source roots bound to this worktree where applicable.

| Check | Raw result |
| --- | --- |
| Final Python preferences/schema unit and integration selection | `499 passed in 44.95s` |
| Final PostgreSQL default, upsert, schema identity and delayed-progress races | `8 passed, 71 deselected in 8.63s` |
| Final frontend preferences, tutorial, mode discovery, settings, App and wire contracts | `10 passed (10)` files; `228 passed (228)` tests |
| Earlier complete frontend run, before the final handoff custody refinement | `272 passed (272)` files; `4845 passed (4845)` tests; the final relevant selection covers the subsequent refinement |
| Focused browser journeys completed before validation was narrowed | 14 distinct selected cases passed across diagnostic reruns; tutorial provider responses mocked, preferences/session API exercised locally |
| Static checks completed before broad gate cancellation | Ruff exit 0; mypy exit 0; contracts exit 0 |
| Frontend checks | TypeScript, ESLint, Stylelint and production build exited 0 |

Python final selection:

```text
tests/unit/web/preferences/
tests/integration/web/test_preferences_routes.py
tests/unit/web/composer/test_preferences_decoder_parity.py
tests/unit/web/sessions/test_schema.py
tests/unit/web/sessions/test_blob_inline_resolutions_schema.py
tests/unit/web/sessions/test_interpretation_events_table.py
tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py
tests/integration/web/composer/guided/test_schema9_epoch.py
tests/unit/contracts/test_web_blob_fencing.py
```

PostgreSQL selection: `tests/testcontainer/web/test_schema_probe_postgres.py -m testcontainer -n 0 -k 'preferences_omitted_mode or preferences_upsert or late_tutorial_progress or postgres_schema_identity_drift'`.

Logs and review working notes are in the primary checkout's `.claude/lanes/composer-freeform-default/`: `targeted-python-final.log`, `targeted-postgres-final.log`, `targeted-frontend-final.log`, backend/frontend reports, and browser run logs. Regression development includes measured failing-before/passing-after evidence for the default, completion contract, and delayed progress writes. Preference mutation-authority pins were rederived with the canonical scanner, not signed or manually fabricated.

The broad gate was stopped at the operator's request before pytest. No full Python or full testcontainer success is claimed. Base keyless lint returned its existing fail-closed corpus; the candidate's global lint stage was interrupted, so no completed global corpus comparison or operator signature acceptance is claimed.

## Release boundary

Session schema epoch is **61**, replacing 60; Landscape schema is unchanged. Deploy the matching frontend/backend together. Existing pre-release session databases require operator-approved recreation; there is no migration or automatic reset. Actual accounts, preferences and session data affected by a shared-store reset must be identified before requesting approval. No shared database was reset and no live provider-backed acceptance was performed for this task.

The operator authorized local release integration and retirement of the feature branch and worktree after scoped validation. The same selections passed on merge commit `9da7ffe340342b3d572f26abf1964c9c2f5b489e`: Python `499 passed in 57.83s`, PostgreSQL `8 passed, 71 deselected in 17.33s`, and frontend `228 passed (228)` across `10 passed (10)` files. All three processes exited 0; logs are `release-python.log`, `release-postgres.log`, and `release-frontend.log` in the lane directory above. The release HEAD stayed unchanged during these runs.

Signing where required, shared-store recreation, publication and deployment remain separate subsequent actions.
