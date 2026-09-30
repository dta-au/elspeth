# Seam: test infrastructure and whole-tree gates — composer async operations (freeform only)

- Tree measured: main checkout, `release/0.8.1`, HEAD `d479eb2b4a559dfae465c6a1c5822003dfda7d2c`; `src/` and `tests/` clean.
- Spec read in full: `docs/specs/2026-09-16-composer-async-operations-design.md` (315 lines).
- Read-only. The only write is this file. Scratch evidence is under the session scratchpad (`lints-base.*`, `tg-*`).
- Host note: two full-suite gates from sibling worktrees were running (load avg 11–20). I ran only one serial focused
  selection (129 passed, 11.6 s) and one key-free lint pass. No broad suite.

---

## 0. The most important finding for the plan author: the route-test client cannot host a worker

**`SyncASGITestClient` starts a new event loop for every request.** `tests/unit/web/_sync_asgi_client.py:41-57`:
each `request()` builds a fresh `AsyncClient(transport=ASGITransport(...))` inside `anyio.run(send)`, and
`:59-71` does the same on a helper thread when called from async code. `__enter__`/`__exit__` (`:31-40`) do
nothing, so **no lifespan runs**.

Measured (throwaway script, no file written): a FastAPI app whose `POST /start` does
`asyncio.create_task(bg())` and whose `GET /probe` inspects that task, driven by `SyncASGITestClient`:

```
{'task_done': True, 'task_cancelled': True, 'bg_ran': False}
distinct loops: 2 of 2
```

So a background task created while handling one request is cancelled when that request returns, and it never
runs. Every existing route-test builder uses a bare `FastAPI()` + `include_router` with no lifespan:

| Builder | Anchor | Client it is used with |
|---|---|---|
| `_route_client` | `tests/unit/web/conftest.py:104-163` (fixtures `test_client` `:166-179`, `closed_local_app` `:201-204`) | `SyncASGITestClient as TestClient` (`conftest.py:58`) |
| `_make_app` | `tests/unit/web/sessions/test_routes.py:853-923` | `TestClient` = `SyncASGITestClient` (`test_routes.py:117`) |
| `_make_progress_route_app` | `tests/unit/web/sessions/test_routes.py:779-802` (service `_ProgressRouteSessionService` `:543`) | `httpx.AsyncClient(ASGITransport)` in the heartbeat tests |

Consequences for the plan:

1. A 202-plus-poll test has to run in one event loop: one `httpx.AsyncClient(transport=ASGITransport(app=app))`
   in a single coroutine. The tree already has this pattern:
   `test_send_message_serializes_concurrent_requests_per_session` (`test_routes.py:6218-6262`, `@pytest.mark.asyncio`)
   and `test_composer_progress_reports_inflight_request_count` (`test_routes.py:4934-4988`, a manual
   `asyncio.new_event_loop()` plus `AsyncClient`).
2. The app-lifespan worker never starts under these builders. The plan needs a fixture or helper that starts the
   worker on `app.state` explicitly inside that loop and drains it on exit. The other route is a lifespan-running
   client such as starlette `TestClient` used as a context manager (as in `tests/unit/web/test_app.py:1145`
   `with TestClient(app) as client:`), but that only works if the fixture app is given a lifespan. None of the three
   builders has one.
3. The tree has at least **131 literal POST call sites to `/messages` or `/recompose` in 10 test files**. Most are
   sync `TestClient` calls that assert `status_code == 200` and read the `MessageWithStateResponse` body inline:
   `response.json()["message"]["content"]`, `body["state"]`, `body["proposals"]`, for example
   `test_routes.py:1439-1450` and `:4856-4862`. Each needs a shared helper, "submit, then drive the worker, then
   poll to terminal, then return the `result` body", which has to run in one loop. Per-file counts, from an AST
   instrument over `git ls-files tests/*.py` that matches `.post(<str|f-str ending /messages or /recompose>)`:

   ```
   107 tests/unit/web/sessions/test_routes.py
     8 tests/integration/web/composer/test_freeform_planner_failure_translation.py
     4 tests/integration/web/composer/guided/test_progressive_disclosure.py
     3 tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py
     2 tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py
     2 tests/unit/web/sessions/test_freeform_mode_persistence.py
     2 tests/unit/web/sessions/test_recompose_admission_refused.py
     1 tests/integration/web/composer/parity/test_repair_and_deferral.py
     1 tests/testcontainer/web/test_composer_splice_concurrency.py
     1 tests/unit/web/test_app.py
   TOTAL 131
   ```

   There are 89 `messages` and 32 `recompose` test functions (test-named functions only).
   - Positive control: it finds `test_send_message_serializes_concurrent_requests_per_session` (`:6219`).
   - Negative control: the same instrument classified 74 `/guided/chat` sites separately and counted none of them
     as freeform.
   - Known blind spots, so treat the count as a **lower bound**:
     - a URL held in a variable, e.g. `_fresh_message_visibility` `path = f".../messages"`;
     - raw-ASGI drives, e.g. `test_client_disconnect_cancels_compose_turn` `test_routes.py:4752-4862`, which builds a
       `scope` with `path=.../messages`;
     - the eval battery's `step("post_message", "POST", ...)`.
4. **`/recompose` has no request body today.** `src/elspeth/web/sessions/routes/composer/compose.py:86-101` (the
   signature has `session_id, request, user, rate_limiter, _inflight_tally` and no body parameter). The frontend sends
   none either: `src/elspeth/web/frontend/src/api/client.ts:917-927` has no `body`. A required `operation_id` means
   `/recompose` gets its first request DTO. Every bodyless `client.post(".../recompose")` in the tests will then
   answer 422, for example `test_routes.py:1701` and `test_recompose_admission_refused.py:52`.

### Callers of the two routes outside the tree's tests (all expect 200 plus a body today)

- `src/elspeth/web/azure_container_apps_observations.py:306-313`: `_fresh_message_visibility` POSTs `/messages`,
  requires `response.status == 200`, and parses `_MessageWrite`. This is production acceptance-probe code; its
  tests are in `tests/unit/web/azure_container_apps_acceptance/test_live_observations.py`.
- `evals/composer-battery/drive_battery.py:384`: `step("post_message", "POST", f"/api/sessions/{sid}/messages", ...)`.
  Its tests are `tests/unit/evals/composer_battery/test_drive_battery*.py`, which use `fake_http.py`.
- Frontend e2e: `src/elspeth/web/frontend/tests/e2e/composer-proposals.spec.ts:184` intercepts
  `POST /api/sessions/{id}/messages` and fulfils a synchronous body.

---

## 1. Whole-tree gates versus the six changes

Change legend:

- **A**: required `operation_id` on the send and recompose DTOs.
- **B**: new SQLAlchemy table plus a session epoch bump.
- **C**: new router module (`GET .../operations/{id}` and `POST .../cancel`).
- **D**: new worker module with asyncio tasks.
- **E**: the two routes change to 202.
- **F**: new `WebSettings` fields.

"Mutation-controlled" means I proved the gate goes red on an in-memory mutation and green on the unmodified tree.

### 1.1 Sessions-schema gates (change B)

| Gate / pin | Anchor | Tripped by | What must move |
|---|---|---|---|
| Exact table-policy set | `tests/unit/architecture/test_session_db_mutation_authority.py:11534-11541` (`test_sessions_metadata_table_policy_is_exact_and_protected`: `Counter(metadata.tables) == Counter(policy.table ...)`) | **B**. Mutation-controlled: adding an in-memory `composer_async_operations` table to `models.metadata` gives `AssertionError` | A `TablePolicy("composer_async_operations", "session", "<Authority>")` in `_TABLE_POLICIES` (`:79-...`). Precedents: `:80` `composer_inflight_requests`/`composer_progress_snapshots` → `SessionComposerProgressAuthority`; `:182-189` `guided_operations` with operation-scoped secondary authorities |
| Named authority registry | same file `:263` `_NAMED_AUTHORITY_SYMBOLS` (the composer-progress precedent is `:290-316`, `SessionComposerProgressAuthority.begin_request/heartbeat_request/...`); `test_named_authority_registry_is_explicit_extensible_and_exact` `:11654-11736` asserts named `operation_authorities` tuples exactly (`guided_operations` `:11720`, `sessions` `:11725`) | **B/D** (new writer class) | One `AuthoritySymbol` per writer method. If the worker's final commit also writes `sessions` or `chat_messages` rows under a new authority name, the `operation_authorities` tuples at `:11711-11731` change too |
| Writer manifest (AST fingerprints plus ordinals plus **line numbers**) | `test_all_production_sessions_writers_are_reviewed_typed_authorities` `:19034-19070` (uses `pytest.xfail` on drift, so it does not hard-fail); `test_live_connection_domain_classification_is_exact` `:18743`; `test_session_schema_authority_is_exact_contained_and_bidirectional` `:19556`. The file has 460 `line=N` literals | **B/D** (new writers). **Also any line shift** in `schema.py`, `service.py` or `repository.py` | New `WriterIdentity` rows. Adding an epoch comment above `schema.py:275` shifts 9 schema.py literals (`:4040-4074` stamp_sentinels 275 / `_stamp_on` 293, 294, 297; `:4954-4974` assert_sentinels 310, validate_required_triggers 423). Precedent: `docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md` §6.1 "Line re-pin" |
| Digest-column shape CHECK | `tests/unit/architecture/test_digest_column_shape_checks.py:216-227` (`test_every_digest_named_column_is_inventoried_or_excluded`; the name regex `_DIGEST_NAME` `:46` matches `hash|sha256|digest|fingerprint|checksum`) plus the per-column behavioural test `:230-...`; the PostgreSQL arm is `tests/testcontainer/web/test_schema_probe_postgres.py:194-236` | **B** (`request_hash`, result SHA-256). Mutation-controlled: `AssertionError('new digest-named columns need a shape in _INVENTORY or a reason in _EXCLUDED')` | An `_INVENTORY` entry (`:71-...`). Precedent: `("sessions","guided_operations"): {"request_hash": "hex64", "response_hash": "hex64"}` (`:85`). Every rejecting CHECK must also exist by name on PostgreSQL |
| Coordination hard-cut sets | `tests/unit/web/sessions/test_schema.py:345-405` (exact `_COORDINATION_HARD_CUT_TABLES` and expiry-index sets) and `:408-498` (exact CHECK-name sets); source `src/elspeth/web/sessions/schema.py:40-54` | **B**, only if the table joins the hard-cut set (a decision; see open questions) | Add it to the `expected_tables`, `expected_indexes` and `expected_checks` dicts |
| Epoch literal pins (`== 67`) | `tests/integration/web/composer/guided/test_schema9_epoch.py:62`; `tests/unit/contracts/test_web_blob_fencing.py:3204`; `tests/unit/web/sessions/test_interpretation_events_table.py:248`; `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py:70`; `tests/unit/web/sessions/test_schema.py:368`; `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py:26` (these 6 files hold the 7 literal hits of `grep -rn -E "SESSION_SCHEMA_EPOCH *== *[0-9]+"`, which counts `test_schema.py:354`'s comment line too) | **B** | Bump each literal. `SESSION_SCHEMA_EPOCH` (`src/elspeth/web/sessions/models.py:366`) and `_COORDINATION_HARD_CUT_EPOCH` (`schema.py:40`) must move together: `schema.py:535-536` raises "coordination schema epoch mismatch" on inequality, and no session DB opens |
| Epoch doc and website sweep | Tests read the live constant and compare it with prose: `tests/unit/docs/test_release_version_surfaces.py:125` (`docs/guides/sharing-pipelines.md:73` `SESSION_SCHEMA_EPOCH=67`); `tests/unit/docs/test_readme_release_surface.py:35`; `tests/unit/website/test_release_site_contract.py:61,167` (`website/get-started.html:106` "53 → 67"); `tests/unit/docs/test_staging_session_recreation_policy.py:21-35,47` (`docs/runbooks/staging-session-db-recreation.md:5,7,217,219,837`); `tests/unit/web/test_azure_container_apps_runbook_contract.py:189,194` (`docs/runbooks/azure-container-apps-deployment.md:109,451,464,538`, `azure-container-apps-cold-install.md:86`); `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py:111,121,179,184` (`docs/runbooks/aws-ecs-deployment.md:1621,1649`); `CHANGELOG.md:39,69,72,239` (the `53\nto NN` line break is asserted) | **B** | A full recipe exists: T6 plus §6.1 of `docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md:660-729, 846-900`. Stale-epoch refusal tests that must stay green: `test_schema.py::test_previous_epoch_rejection_does_not_rewrite_store` (`:521`), `test_engine.py::test_initialize_session_schema_rejects_stale_user_version`; PostgreSQL siblings in `test_external_deployment_postgres.py` and `test_schema_probe_postgres.py:397` |
| Soft-mapping census | `config/cicd/soft-mapping-census.yaml` (current: `routes/messages.py` `dict[str, Any]: 1` `:1063-1064`; `routes/composer/compose.py` `dict[str, Any]: 1` `:1041-1042`; `routes/_helpers.py` 17 soft sites `:1037-1040`; totals `soft: 2844` `:1081`); enforced by `scripts/check_contracts.py` (pre-commit `Check Contracts`, CI "Enforce contract alignment", `ci.yaml:382`) | **A/B/C/D** whenever an annotation of the form `dict/Mapping/MutableMapping[str, Any\|object]` is added, moved or removed. Stored request and result JSON parsing tends to produce these | `python -m scripts.check_contracts --write-census` in the same commit; never hand-edit. A parameter parsed at a `@trust_boundary` counts in the `boundary` column (a removal) |

### 1.2 Route and dependency inventories (changes C and E)

| Gate / pin | Anchor | Tripped by | What must move |
|---|---|---|---|
| IDOR ownership inventory | `tests/unit/web/sessions/test_routes.py:3676-3880`. `EXPECTED_SESSIONS_OWNERSHIP_ENDPOINTS` `:3718-...` contains `"send_message"` and `"recompose"` (`:3724-3725`). `test_sessions_routes_ownership_call_sites` `:3861-3874` walks a **fixed module tuple** `(sessions, state, proposals, compose, guided, messages, runs, interpretation)` (`:3863-3866`) | **C**. **A new route module is invisible** unless it is added to that tuple, so this is a silent coverage gap rather than a red | Add the new module to the tuple and the new handler names (GET operation, POST cancel) to the inventory. If `_verify_session_ownership` stops being called directly inside `send_message`/`recompose` (because it moves to a dependency or the worker), those names leave the inventory |
| IDOR cross-session walk | `TestIDORProtection.test_idor_session_crud` `test_routes.py:3961-4100+` (the docstring says every ownership-gated endpoint must have a cross-session assertion; `/recompose` is listed at `:3983`) | **C** (by convention; the inventory test above is what enforces it) | Bob's GET operation and POST cancel on Alice's session must return 404 |
| Mounted dependency pins | `tests/unit/web/sessions/routes/test_composer_request_telemetry.py:116-121` (`/guided/respond` mounts `_track_compose_inflight` exactly once); `tests/testcontainer/web/test_cross_process_composer_postgres.py:83-85` (`post_guided_plan` mounts it) | Not tripped when only the two freeform routes drop it (guided pins only). **No pin exists that freeform routes mount it**, so removing the mount is silent here | Keep the guided pins unchanged. They are the "guided behaviour unchanged" evidence |
| Surface label | `test_composer_request_telemetry.py:91-113` parametrizes `("/api/sessions/1/messages", "freeform")`, driving the dependency directly (`_settle_dependency` `:41-87`); production `_helpers.py:2485` `surface: Literal["freeform", "guided"] = "guided" if "/guided/" in request.url.path else "freeform"` | **E**. It stays green after unmounting, which leaves the `freeform` label dead in the dependency. `_track_compose_inflight`'s docstring `_helpers.py:2449` names both freeform routes (prose drift) | Decide whether the worker lifecycle emits `surface="freeform"` metrics |
| Route-split module list | `tests/unit/web/sessions/test_routes_split.py:10-21` (imports a fixed module set), `:72-83` (no `import *` under `routes/`) | **C** (only if the new module lives under `routes/`; no hard fail) | Optional: add it to the set |
| App route presence | `tests/unit/web/test_app.py:1476-1484` (`test_execution_routes_registered`; positive membership only) | Not tripped | **No exact route-set inventory was found** |
| OpenAPI | `tests/unit/web/composer/guided/test_no_chain_authoring_path.py:83-100` scans `app.openapi()` for retired tokens (`_RETIRED_CONTRACTS`, including the substring `chain_in`); `:163` asserts `/guided/plan` exists | Not tripped, unless a new schema or field name contains a retired token | **No OpenAPI snapshot or golden was found.** `tests/golden/` has only `state_engine/` and `web/catalog/`; the frontend `generate-types` script (`package.json`) writes `src/types/api.generated.ts`, which is **not committed** |
| Structural "route owns the lease" pins | `tests/unit/web/sessions/test_operation_fence_wiring.py:94-120` (`test_send_message_acquires_compose_authority_before_state_or_message_access`: requires, inside `send_message` within `register_message_routes`, an `async with compose_lock, SessionOperationLease.acquire(...) as compose_operation_lease`, and that lease must precede `service.get_current_state` and `service.add_message_with_transcript(session_operation_context=compose_operation_lease.context)`); `:344-360` (`test_auto_title_is_owned_by_and_reuses_the_compose_lease`: `maybe_auto_title_session(..., session_operation_context=compose_operation_lease.context)` wrapped in `compose_operation_lease.create_task`); `tests/unit/web/composer/guided/test_no_chain_authoring_path.py:131-146` (`recompose` scope calls `settle_auto_commit_intent` exactly once and `settle_pipeline_proposal_under_compose_lock` zero times) | **D/E**. They go red, or worse, `next(...)` raises `StopIteration`, the moment the compose body leaves the route function | Re-target them at the worker's turn function **with the same intent**. These are design constraints, not churn: the worker must acquire `SessionOperationLease` (COMPOSE) before any state read or transcript write, and must own auto-title through that lease |
| Inflight-count semantics | `test_routes.py:4934-4988` asserts `inflight_requests == 1` while a `/messages` compose is parked, then `0` after | **E** (unmounting `_track_compose_inflight` from `/messages`) | The spec (§2) makes progress advisory. The test must change or be replaced by a poll-state assertion |

### 1.3 Dynamic-attribute, masquerade, mock and settings gates

| Gate / pin | Anchor | Tripped by | Notes |
|---|---|---|---|
| Attribute contracts (sessions and composer) | `tests/unit/web/test_sessions_composer_attribute_contracts.py:61-141` (walks `src/elspeth/web/sessions` and `src/elspeth/web/composer` via `iter_gate_sources`; the expected set is only the `_admit_*` and `_capture_composer_llm_completion_fields` LiteLLM boundaries) | **C/D** if any `getattr`/`hasattr`/`getattr_static`/`__getattr__` is added under those two roots | A worker in `web/coordination/` or a new `web/<pkg>` is outside this gate's walk but **inside** the masquerade gate below. Use direct attribute access on owned types |
| Masquerade baseline (whole repo, **tests included**) | `tests/unit/elspeth_lints/test_masquerade_gate.py`; baseline `config/cicd/masquerade_baseline.yaml`; reseed `python -m elspeth_lints.rules.masquerade.seed_baseline` then `--check` | **Any** new `getattr`/`hasattr` in src or tests (for example `getattr(app.state, "async_operations", None)` in a fixture) | Parametrize by objects, not attribute names |
| Unspecced mocks (whole repo) | `tests/unit/test_mock_discipline_baseline.py:1-40` (zero tolerance; `MOCK_NAMES` × `SPEC_KEYWORDS = {autospec, spec, spec_set, wraps}`) | New tests | Existing freeform fakes are compliant (`_make_composer_mock` `test_routes.py:351-365` uses `AsyncMock(spec=ComposerService.compose, ...)`) |
| Gate walker | `tests/unit/elspeth_lints/test_python_file_walker_authority.py`: no literal `rglob("*.py")` or `os.walk(` under `tests/`; scratch tests only in `tests/_scratch/` | New gate tests | Use `iter_gate_files`/`iter_gate_sources` from `tests/helpers/tree_gate.py` |
| `WebSettings` blank-string validators | `tests/unit/web/test_config.py:1533-1560` (`_optional_string_field_names` → every `str \| None` field must reject blanks) | **F**, only for `str \| None` fields | Also `tests/unit/web/test_app.py:2688,2697` enumerate tuple-typed fields |
| Timeout/transport coupling | Validator `src/elspeth/web/config.py:1186-1208` (`_validate_composer_timeout_transport_headroom`); fields `:325`, `:335-352`. Tests: `tests/unit/web/test_config.py:242-260` (`test_composer_timeout_must_leave_transport_headroom`), `tests/unit/web/test_app.py:764-773`, `tests/unit/web/composer/test_tutorial_service.py:660-693` (`test_live_tutorial_wait_uses_transport_ceiling_minus_headroom`), `tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py` | **F** / spec §3 relationship change | ECS: `tests/unit/deployment/test_aws_ecs_terraform_package.py:3365-3470` (`test_composer_wall_clock_fits_under_the_app_guard_and_the_alb`) regex-pins the `var.composer_timeout_seconds` cap `<= var.alb_idle_timeout_seconds - 30` in `deploy/aws-ecs/terraform/modules/scenario/variables.tf:622-636` and the env wiring in `locals.tf:476-482` |
| Shipped `ELSPETH_WEB__` floor | `tests/unit/deployment/test_aws_ecs_terraform_package.py:3643-3700+` (`test_documented_minimum_image_revision_is_the_true_settings_floor`, against `deploy/aws-ecs/terraform/README.md` "the image must include commit `<sha>`"); `:3570` (every shipped name is a real field); `tests/unit/deployment/test_web_settings_exports_resolve.py:97` | **F**, only if a new field is **shipped** in a deploy env or template | Defaulted-only fields do not move the floor |
| Settings/Runtime contract alignment | `scripts/check_contracts.py` checks 3 and 4 scan `src/elspeth/core/config.py` and `contracts/config/runtime.py` only (`check_contracts.py:1799+`, lines `config_path = src_dir / "core" / "config.py"`) | Not tripped by **F** | `WebSettings` is out of that scope |
| Landscape mutation fencing | `tests/unit/architecture/test_web_landscape_mutation_fencing.py` (inventory pins over web → Landscape mutation calls) | Not expected to trip. The composer LLM-call audit lands as `chat_messages` `role="audit"` rows in the sessions DB (`test_routes.py:440-450`), not in Landscape | Confirm with `python scripts/fencing_inventory.py .` if the worker gains any Landscape call |
| Wire-shape templates, plugin hashes, runtime-rejection parity, CSS barrel | CONTRIBUTING §§ Gate: wire-shape templates / plugin inventories / runtime-rejection parity / CSS barrel | Not tripped by A–F as scoped (no `no_tool_policy.py` suffix, no plugin, no `core/dag` or `core/config.py` raise) | CSS barrel applies only if the frontend adds stylesheet tokens (`npm --prefix src/elspeth/web/frontend test`) |

### 1.4 Baseline measurement of the focused gates (negative control)

```
python -m pytest -n 0 -q -p no:cacheprovider \
  tests/unit/web/test_sessions_composer_attribute_contracts.py \
  tests/unit/architecture/test_digest_column_shape_checks.py \
  test_session_db_mutation_authority.py::{test_sessions_metadata_table_policy_is_exact_and_protected,test_named_authority_registry_is_explicit_extensible_and_exact} \
  test_schema.py::{test_current_schema_includes_coordination_hard_cut_tables_and_expiry_indexes,test_coordination_hard_cut_check_constraints_are_exact} \
  test_operation_fence_wiring.py::{test_send_message_acquires_compose_authority_before_state_or_message_access,test_auto_title_is_owned_by_and_reuses_the_compose_lease} \
  test_routes.py::TestIDORCoverageDrift  test_routes_split.py \
  test_no_chain_authoring_path.py::test_reachable_surfaces_share_one_lock_assuming_commit_boundary
129 passed in 11.59s
exit=0
```

Positive control: the in-memory mutation (adding a `composer_async_operations` table with `request_hash` and
`result_sha256` to `models.metadata`) turned red both `test_sessions_metadata_table_policy_is_exact_and_protected`
(`AssertionError`) and `test_every_digest_named_column_is_inventoried_or_excluded`.

---

## 2. Route-test anatomy: app, auth, provider fakes, response assertions

- **App construction.** Bare `FastAPI()` plus `app.include_router(create_session_router())`
  (`src/elspeth/web/sessions/routes/__init__.py:44-52`; composer sub-routers are registered in
  `routes/composer/__init__.py:16-19`, `state`, `proposals`, `compose`, `guided`). State is set by hand:
  - `session_service`: `DualFencedSessionServiceHarness` in `_make_app`/`_route_client`;
    `_ProgressRouteSessionService` in `_make_progress_route_app`.
  - `settings`: `WebSettings(composer_timeout_seconds=85.0, ...)`.
  - `composer_service = None`, which each test replaces.
  - `composer_progress_registry`: `ComposerProgressRegistry()` in `_make_app` and the progress app; `None` in
    `_route_client`.
  - `rate_limiter`, `execution_service` stub, `payload_store`, `scoped_secret_resolver`.
  - Anchors: `tests/unit/web/conftest.py:118-161`, `test_routes.py:779-802, 853-923`.
- **Auth.** `app.dependency_overrides[get_current_user] = mock_user`, which returns a fixed
  `UserIdentity(user_id="alice", ...)` (`conftest.py:120-125,146`; `test_routes.py:786-790, 882-890`). The identity
  row is seeded with `ensure_test_identity` from `tests/fixtures/identities`. The cross-process PostgreSQL harness
  overrides `_helpers.get_current_user` (`test_cross_process_composer_postgres.py:63`).
- **Provider fakes.** Tests replace `app.state.composer_service`, which is duck-typed by
  `compose(message, chat_messages, state, *, session_id, current_state_id, user_id, progress, guided_terminal, user_message_id, session_operation_context, completion_gates)`:

  | Fake | Anchor | How it holds or behaves |
  |---|---|---|
  | `_make_composer_mock(response_text, state)` | `test_routes.py:351-365` | `SimpleNamespace` with specced `AsyncMock` `compose` and `surface_pending_interpretation_reviews` returning `ComposerResult`. Returns at once |
  | **`_BlockingRecordingComposer`** | `test_routes.py:453-495` | **Holds on `asyncio.Event`s, not sleep.** It exposes `first_call_started`, `second_call_started` and `release_first_call` (`:458-460`) and records `calls`. Use: `test_routes.py:6218-6262`, which does `asyncio.create_task(send(...))`, then `await asyncio.wait_for(composer.first_call_started.wait(), 1.0)`, then proves the second call is queued (`wait_for(..., 0.3)` raises `TimeoutError`), then `composer.release_first_call.set()` and `gather`. **This is the fixture to reuse or generalize** for the "provider held beyond 125 s" gate: hold on `release`, assert the POST already returned 202, poll, release, poll to terminal. Caveat: it is a module-local class in `test_routes.py` (other files import from `tests.unit.web.sessions.test_routes`, e.g. `test_recompose_admission_refused.py:22`, `test_compose_heartbeat_renewal.py:41`). A shared home under `tests/fixtures/` would be cleaner |
  | `_ParkedComposer` (inline) | `test_routes.py:4955-4959` (inside `test_composer_progress_reports_inflight_request_count`) | `compose_started.set(); await release.wait(); return await inner.compose(...)`. It wraps `_make_composer_mock` |
  | `_HangingComposer` | `test_routes.py:4774-4788`; `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py:30-45` | Sets `started`, parks on `asyncio.Event().wait()` forever, and records `cancelled`. Good for Stop and cancellation-audit tests |
  | `_ProgressAwareComposer` | `test_routes.py:498-540` | Asserts `session_operation_context` and `progress` are passed and publishes a `ComposerProgressEvent` |
  | Heartbeat fake clock and registry | `test_compose_heartbeat_renewal.py:59-104` (`_FakeHeartbeatClock`, `_ScriptedRegistry` with an `asyncio.Event` `renewed`), installed via `monkeypatch.setattr(_helpers, "_COMPOSER_HEARTBEAT_TIMER", _helpers._ComposerHeartbeatTimer(now=..., wait=...))` (`:112-117`) | Time never sleeps. Production knobs are `_COMPOSER_HEARTBEAT_SECONDS = 15.0` (`_helpers.py:2338`), `_COMPOSER_REQUEST_LEASE_SECONDS = 60` (`:2340`), `_ComposerHeartbeatTimer` (`:2351`), `_COMPOSER_HEARTBEAT_TIMER` (`:2363`), `_ComposerHeartbeatCancel` (`:2367`), `_composer_heartbeat_cancel_of` (`:2395`). The extracted operation-owned lifecycle (spec §3) should keep an injectable timer so these tests port without real sleeps |
- **Response assertions today.** The tests assert synchronously on the POST response: `status_code == 200`, then
  `body = response.json()`, then `body["message"]["content"]`, `body["state"]` (null or a state dict with `id`), and
  `body["proposals"]` (a list). Examples: `test_routes.py:1439-1450`, `:4856-4862`. Error tests read
  `response.json()["detail"]`, e.g. `test_recompose_admission_refused.py:42-54` and the heartbeat 503 body
  `{"detail": {"error_type": "composer_request_lease_lost", ...}}` at `test_recompose_heartbeat_cancel.py:111-118`.
  The DTO is `MessageWithStateResponse(_StrictResponse)` at `src/elspeth/web/sessions/schemas.py:228-237`
  (`message`, `state=None`, `proposals`; `_StrictResponse` is `strict=True, extra="forbid"`, `:54-65`). Under 202,
  each of these becomes "poll `GET .../operations/{id}` until terminal, then compare `result` (or `error`)".
  `MessageWithStateResponse.model_validate(result)` gives a cheap exactness check.
- **Heartbeat tests to adapt (spec gate 4).**
  - `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` (512 lines; drives the dependency in a child
    task via `_run_fake_route` `:120-164`; its module docstring explains why the owner must be a separate task).
  - `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py` (143 lines; asserts 503, a failed progress
    event and the terminal counter `{"endpoint": "recompose", "status": "failed"}`).
  - `tests/unit/web/sessions/routes/test_composer_request_telemetry.py` (261 lines).
  - **Guided heartbeat tests must stay byte-unchanged:**
    `tests/integration/web/composer/guided/test_guided_heartbeat_cancel_classification.py:178-343` (plan, chat and
    respond × heartbeat cancel, plain cancel, disconnect).
- **Disconnect or cancel tests** that encode today's 499 contract for `/messages`, which the spec retires for the two
  routes: `test_routes.py:4752-4862` (`test_client_disconnect_cancels_compose_turn`, raw ASGI, asserts 499 and
  `composer.cancelled is True`) and `:4865-4932` (`test_external_cancel_racing_disconnect_keeps_unwinding`, a unit
  test of `_cancel_on_client_disconnect`, which the guided routes still use and so keeps).

---

## 3. PostgreSQL testcontainer harness

- **Shared container and xdist rejection.** `tests/testcontainer/web/conftest.py:31-36`
  (`_require_sequential_postgres_acceptance` raises `pytest.UsageError` under an xdist worker), `:39-129`
  (session-scoped `external_deployment_postgres_url`: one TLS PostgreSQL built with a throwaway CA, or the provisioned
  `ELSPETH_TEST_POSTGRES_URL` via `tests.helpers.postgres_target`). The rejection binds only to tests that request
  this fixture. Marker `pytestmark = pytest.mark.testcontainer`; CI job `ci.yaml:895-947` runs
  `pytest tests/ -m testcontainer -n 0` (`pyproject.toml:455` default addopts deselect `testcontainer`).
- **Two `postgres_engine` fixtures with different scopes:**
  - `tests/testcontainer/web/test_session_operation_fence_postgres.py:132-138`: `scope="module"`, built on the
    shared `external_deployment_postgres_url`, with `initialize_session_schema` once per module.
  - `tests/testcontainer/web/test_schema_probe_postgres.py:84-104`: a module-scoped **own** container
    (`postgres_url` via `postgres_test_target(driver="psycopg")`) plus a **function-scoped fresh database per test**
    (`CREATE DATABASE elspeth_schema_<hex>` … `DROP DATABASE ... WITH (FORCE)`). This one is better for schema, CHECK
    and epoch tests.
- **Cross-instance harness (two app instances on one PostgreSQL).**
  `tests/testcontainer/web/test_cross_process_composer_postgres.py`:
  - `_http_lifecycle_process` / `_serve_lifecycle_commands` (`:44-134`): each **spawned process**
    (`multiprocessing.get_context("spawn")`) builds its own `create_session_engine(url)`, its own
    `DatabaseComposerProgressRegistry(SessionComposerProgressAuthority(engine, owner_instance_id=uuid4().hex, lease_seconds=2))`
    and a **real FastAPI app** (`state.router` + `guided_plan.router` plus a test probe route with
    `Depends(_helpers._track_compose_inflight)` that parks on `asyncio.Event().wait()`). It serves pipe commands
    (`start`/`abort`/`poll`/`stop`) through one long-lived `AsyncClient(ASGITransport)` in **one event loop**
    (`:87-128`). A fresh `AsyncClient` per `poll` models a reload or reconnect (`:108-113`).
  - `_composer_process` (`:161-197`) plus `_workers` context manager (`:210-233`): two spawned processes, each with
    an independent engine and authority, driven by `(command, argument)` tuples. `_receive` gives a 30 s bound per
    reply (`:200-202`).
  - Test shapes to copy:
    - `test_fastapi_abort_heartbeat_and_reload_share_durable_lifecycle_across_processes` (`:242-292`): two app
      processes on one PostgreSQL, real heartbeat renewal across a `pg_sleep(2.5)`, abort, and reload.
    - **`test_killed_publisher_leaves_committed_snapshot_for_fresh_peer` (`:507-537`)**: `owner.kill()`, asserts
      `exitcode < 0`, waits out the lease with `SELECT pg_sleep(2.1)`, then fresh peers read the committed state.
      This is the template for spec gate 3 ("kill a worker in each crash window").
    - `test_replay_cas_has_one_winner_and_queued_work_blocks_replay` (`:540`).
  - Heartbeat is shortened by `monkeypatch.setattr(_helpers, "_COMPOSER_HEARTBEAT_SECONDS", 0.2)` inside the child
    (`:86`) with `lease_seconds=2`.
- **Race harness in one process (CAS winners).**
  - `test_session_operation_fence_postgres.py:350-371` (`test_postgres_two_claimants_have_exactly_one_winner`):
    `ThreadPoolExecutor(max_workers=2)` with two independent `PostgresSessionOperationRepository(postgres_engine)`
    contenders, asserting one `SessionOperationContext` and one `SessionOperationConflictError`. Its model for
    COMPOSE exclusion is `repository.acquire(session_id=..., operation_kind=SessionOperationKind.COMPOSE, owner_instance_id=..., lease_seconds=30)`
    (`:62-67`).
  - Other anchors in the same file: `_register_instance` (`:149-167`) inserts `web_instances` rows (with a
    hard-coded `session_epoch=37`); `test_postgres_expiry_takeover_requires_operation_and_owner_instance_expiry`
    (`:276`); `test_postgres_waiter_cannot_act_after_expiry` (`:833`).
  - Guided-operation takeover precedents, the closest analogue to queued-claim/reclaim:
    `test_schema_probe_postgres.py:512` (`test_postgres_guided_operation_takeover_fences_late_worker`), `:649-744`
    (`test_postgres_concurrent_expired_reserve_has_one_takeover_winner`, three `SessionServiceImpl` contenders on one
    engine), `:745` (takeover decided at the session fence), `:1150` (locator CHECK constraints on PostgreSQL).
- **Digest CHECKs on PostgreSQL** are automatic once the columns are in the digest gate `_INVENTORY`
  (`test_schema_probe_postgres.py:194-236`).

---

## 4. Trust tier: rules that apply to the worker's parsing and the new modules

**How to run key-free** (confirmed: `env | grep -c ELSPETH_JUDGE_METADATA_HMAC_KEY` → `0`):

```
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth
# CI-only companions after any @trust_boundary / @observation_boundary edit or an edit to its test_ref:
.venv/bin/python -m elspeth_lints.core.cli check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth
```

**Baseline captured** at `d479eb2b4`: exit=1 (the deliberate fail-closed state), 2355 non-WARNING lines, 2 m 59 s
wall. By `$4` field:

- `R_TB_SUPPRESSED` 1680
- `trust_tier.tier_model` 247 (allowlist/stale/expired lines)
- `R5` 244
- `R6` 113
- `R1` 21
- `R2` 17
- `R4` 16
- `R9` 5
- `R7` 5
- `allowlist.unused_rule` 3

Saved in the session scratchpad as `lints-base.findings`. **Diff finding sets, not counts**, and rebaseline after
merging the landing branch.

**Registered rules** (from `DEFAULT_REGISTRY.load_builtin_rules()`, 24 ids) that the new code can trip:

| Rule | Scope | Why it matters here |
|---|---|---|
| `trust_tier.tier_model` R1–R9, L1, TC (`elspeth-lints/src/elspeth_lints/rules/trust_tier/tier_model/rule.py:277-340`) | whole repo | Parsing stored `request_json`/`result_json` is Tier-3-ish session data. The house shape is one declared boundary per module: `@trust_boundary(source_param=..., suppresses=("R1","R5"), invariant=..., test_ref=..., test_fingerprint=...)`. It can suppress **only R1 and R5**; R2, R4, R6, R7 and R9 need a code change or a judged allowlist entry. The derived-name trail is lost across `try:` bodies, closures and `zip`/`enumerate` targets (CONTRIBUTING "Convention: trust-tier rules"). Prefer constructing the owned DTO with `MessageWithStateResponse.model_validate_json(...)` / `SendMessageRequest.model_validate_json(...)` rather than hand-walking dicts |
| **R5 route-handler exemption** (`rule.py:1490-1496` `_is_fastapi_route_handler`; used by `_is_allowed_r5_context` `:1583-1592`) | n/a | `isinstance` is exempt **only** inside a `web/` function decorated with a FastAPI method decorator, a pydantic before-validator, a Tier-1 frozen-dataclass `__post_init__`, or a named boundary in `_R5_NAMED_BOUNDARY_CONTEXTS`. Today `messages.py:734` and `compose.py:501` (`isinstance(current_exc, asyncio.CancelledError)`) are exempt because they sit inside `send_message`/`recompose` (AST measurement: each route body has exactly 1 `isinstance`, 0 `.get`, 0 broad `except`; positive control `post_guided_plan` has 6 `isinstance` and 8 broad `except`). **In a worker module they become new R5 findings.** |
| R7 allowlist entries tied to `recompose` | `config/cicd/enforce_tier_model/web.yaml:7401` and `:7420` (`web/sessions/routes/composer/compose.py:R7:recompose:fp=5aa81ae8e862047c` / `fp=c141c6136689849c`, bound by `scope_fingerprint` of the whole `recompose` function) | **Already reported `Stale tier-model allowlist entry` at baseline**, and `compose.py:792` / `:809` `contextlib.suppress(asyncio.CancelledError)` are live R7 findings in the baseline. Moving that shielded-audit-persist pattern into the worker yields new R7 keys in the worker file and `stale_delete` of these two, which the operator re-signs. `messages.py` has no `contextlib.suppress` and no allowlist key |
| `audit_evidence.tier_1_decoration` | whole repo | Every new exception class (claim lost, operation conflict or mismatch, and so on) must be marked Tier-1 or explicitly justified as Tier-2 |
| `audit_evidence.guard_symmetry` | whole repo | A dataclass with post-init validation (for example the operation-row record) needs a matching read-side `AuditIntegrityError` loader guard |
| `immutability.freeze_guards` / `immutability.frozen_annotations` | whole repo | A frozen worker-context dataclass with container fields must use recursive freeze guards and immutable annotations |
| `contract_invariants.portable_sqlite_insert` | incremental | SQLite-specific insert builders (e.g. `sqlite.insert(...).on_conflict_do_nothing()` for the ID reservation) need an explicit PostgreSQL counterpart and dialect dispatch. The PostgreSQL `FOR UPDATE SKIP LOCKED` versus SQLite CAS claim is exactly a dialect-dispatch site |
| `contract_invariants.session_engine_factory` | incremental | Engines only through `create_session_engine` (tests too: `create_engine` appears only in the PostgreSQL admin fixture) |
| `masquerade.attribute-probes` | whole repo | See 1.3 |
| `composer.exception_channel` / `composer.catch_order` | incremental | Tool-plane modules only (`ToolArgumentError`). `catch_order` applies if the worker's error adapter catches `ComposerServiceError` subclasses: subtype handlers must precede their supertypes in one `try` |
| ADR-032 (`docs/architecture/adr/032-validate-by-trust-domain.md`) | convention | Nominal `isinstance`/`type(x) is C` for owned types; no `runtime_checkable` Protocol for dispatch. pydantic passes nested dicts by identity even under `strict=True` |

**Codec precondition.** The spec names the "model to follow" as `guided_operation_request_hash`
(`src/elspeth/web/sessions/guided_operations.py:17-44`). At `:25-29` it raises `AuditIntegrityError` unless the
DTO's `model_config` has `strict is True` **and** `extra == "forbid"` **and** an `operation_id` field. Today's
`SendMessageRequest` (`schemas.py:145-160`) derives from `_RequestModel` (`:68-71`, `extra="forbid"` only, coercion
allowed), not from `_GuidedOperationRequest` (`:74-89`, `strict=True, extra="forbid"`,
`operation_id: str = Field(min_length=36, max_length=36)` plus a canonical-UUID validator). Strict DTOs in this tree
parse UUID fields with a `mode="before"` validator (`ForkSessionRequest._parse_from_message_id` `:390-403`,
`RevertStateRequest._parse_state_id` `:420-...`). That is also an R5-exempt context (pydantic before-validator).
Moving `SendMessageRequest.state_id: UUID | None` (`:155`) to strict therefore needs the same before-validator.

---

## 5. Frontend gates in CI

`.github/workflows/ci.yaml`:

| Job | Anchor | Commands (working dir `src/elspeth/web/frontend`) |
|---|---|---|
| `frontend-unit` "Frontend unit (vitest + typecheck)" | `ci.yaml:1296-1326` | `npm ci`; **`npm run typecheck`** (= `tsc -p tsconfig.app.json --noEmit && tsc -p tsconfig.oidc.json --noEmit`); **`npm test -- --run`** (= `vitest run`) with `CI=true` |
| `e2e-frontend` "Frontend E2E (Playwright)" | `ci.yaml:1197-1287` | `uv sync --frozen --all-extras`; `npm ci`; `npx playwright install chromium`; isolated ports `PLAYWRIGHT_BACKEND_PORT`/`PLAYWRIGHT_FRONTEND_PORT`; **`npm run test:e2e`** (= `playwright test`) |
| `ci-success` | `ci.yaml:1331-1383` | Requires both frontend jobs |

- `npm run lint` (eslint over `src` plus named e2e files) and `npm run lint:css` (stylelint) are defined in
  `package.json` but **not invoked by any job in `ci.yaml`**. I grepped `.github/workflows/*` and found no other
  `npm run` caller.
- `npm run build` (= `tsc -p tsconfig.app.json --noEmit && vite build`, then `postbuild` prune) runs only in the
  image build (`Dockerfile:46`). Run lint and build locally anyway.
- Playwright auth state is shared per worktree (CONTRIBUTING "Gate: Playwright auth state"): run suites sequentially.
- No runtime decoder guards `MessageWithStateResponse`. The freeform calls use `parseResponse<MessageWithStateResponse>`
  (`client.ts:901-927`); `exactRecord` strict decoders exist only in `src/api/guidedDecoder.ts` (94 uses) and
  `src/api/preferencesDecoder.ts` (2). A new poll-response type has no existing decoder to extend. Whether to add one
  is a design choice.
- Frontend tests touching `sendMessage`/`recompose`:
  - `src/stores/sessionStore.test.ts` (152 references)
  - `src/hooks/useComposer.test.ts` (9)
  - `src/api/client.recovery.test.ts` (6)
  - plus `App.test.tsx`, `ChatPanel*.test.tsx`, `stores/subscriptions.test.ts`, `SideRailValidationBanner.test.tsx`,
    `test/inlineSourceIntegration.test.tsx`, `commandRegister`/`CommandPalette` tests
  - e2e `tests/e2e/composer-proposals.spec.ts:184` (a mocked synchronous POST)

---

## 6. Recommended canonical verification for the plan

- Scoped: the focused set in 1.4 plus the new async tests, run `-n 0` for anything that holds events.
- Pre-merge: `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer`.
  The default stages are only `ruff,pytest`. Read `summary.txt` and require `frozen=yes`. Testcontainer is mandatory
  because of B and the claim SQL.
- Lints: diff against a baseline taken on the merged landing tip. List additions for the operator's sign-bundle.
  Never hand-edit signatures.
- Frontend: `npm run typecheck && npm test -- --run && npm run lint && npm run build`, then Playwright sequentially.
