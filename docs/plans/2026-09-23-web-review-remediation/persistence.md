# Persistence, credentials, provenance and deployment remediation plan

Read-only planning, 2026-09-23. Baseline inspected: `1e9cafa9d` on `release/0.8.1`; review pin: `74c0ce0db`. No production edits, commits or tests were performed by this lane. Owned issue set: R05, R07, R08, R09, R11, R12, R13, R35, R36, R50, R51, R52, R59, R71, R72, R73.

## Current evidence and coordination

`git diff --stat 74c0ce0db..HEAD -- src/elspeth/web/auth src/elspeth/web/coordination src/elspeth/web/execution src/elspeth/web/shareable_reviews src/elspeth/web/config.py deploy/compose deploy/linux-systemd docs/guides/docker.md docs/reference/environment-variables.md` printed:

```text
 .../coordination/composer_progress_authority.py    |  63 +++++++++----
 src/elspeth/web/execution/routes.py                |  70 +++++++++-----
 src/elspeth/web/execution/websocket_close.py        | 104 +++++++++++++++++++++
 3 files changed, 199 insertions(+), 38 deletions(-)
```

Direct reads confirm all defects assigned to this lane remain in the inspected live sources. This is source evidence, not a fresh behavioral reproduction. R05 progress code has moved: `git log 74c0ce0db..HEAD -- .../composer_progress_authority.py .../execution/routes.py` lists `33abfd48f`, `f2b43cfdc`, `7c2acc588`, `3e66806bc`; preserve their opaque authorization denials, consistent read snapshots and cancellation-shielded teardown. Admission still reads session without `FOR UPDATE` at chargeable_admission_authority.py:36–38 and then locks identity at :39–43. Settlement still locks attempt then reads session without locking at quota_authority.py:184–189.

`git worktree list` shows an existing `fix/credential-generation-fence` at `601d6a9a9`. `git merge-base --is-ancestor 601d6a9a9 HEAD` returned exit 1. Current local.py:676–688 still has the unfenced deletion closures. Before the auth writer starts, reconcile this existing prepared change and its ownership; do not implement a competing credential-generation fix. Its original work included replacement-registration concurrency evidence, but historical test counts are not current-tree proof.

The active `.claude/worktrees/pre-publication-security` is at `1e9cafa9d` and has dirty CI, CONTRIBUTING, judge-handoff, `composer/guided_blob_refs.py`, `composer/tools/sessions.py`, `composer/yaml_generator.py`, and related tests. The tracked security brief covers CI secret custody and sentinel projection, not R07–R09. Avoid those dirty paths; coordinate if a provenance or pipeline lane touches source custody. No duplicate security dispatch is needed.

## Recommended writers and ownership

1. **P1 database lock order:** R05, one writer plus a PostgreSQL reviewer. Own chargeable_admission_authority.py, quota_authority.py and their targeted tests. Progress/ticket files only for precise contract comments or necessary fixes; preserve post-pin changes.
2. **P2 credential authority and audit:** R07/R08/R09/R59 together, one writer plus a security/audit reviewer. Own auth/admin_routes.py, local.py, auth/routes.py, auth/audit.py, identity_authority.py, identity_lifecycle.py, app.py wiring, contracts/auth.py, related CLI wiring and auth tests. Reconcile existing generation-fence commit first. No frontend permission rollback: all live local administrators remain eligible.
3. **P3 execution policy and recovery:** R12/R13/R50/R51, one writer plus an execution reviewer. Own execution/service.py, execution/recovery.py, run_recovery_authority.py, web/config.py rate-limit sections and their tests. These share execution/service.py and should not be split into parallel writers.
4. **P4 interpretation provenance:** R35/R36 together, one writer plus a hash/provenance reviewer. Own the interpretation portions of coordination/repository.py, sessions/pending_interpretation.py, composer/service.py and contracts/composer_interpretation.py. Coordinate exclusive turns with the R03/R14/R22/R25/R26 state-writer lane on repository.py, sessions/service.py and composer/service.py. Use explicit section ownership only if both writers agree; otherwise serialize patches.
5. **P5 deployment configuration/docs:** R11/R71/R72/R73, one writer plus a deployment reviewer. Own deploy/compose, deploy/linux-systemd comments, docs/guides/docker.md, docs/reference/environment-variables.md and deployment tests. Synchronize web/config.py edits with P3; any CLI forwarded-header setting also shares cli.py with P2.
6. **P6 share snapshot refusal:** R52, small independent writer/reviewer, own shareable_reviews/service.py and its tests.

Central schema owner integrates epochs and parity changes from P2/P4 alongside R02; no lane independently races epoch constants or edits others' schema files. Current `SQLITE_SCHEMA_EPOCH` is 43 in core/landscape/schema.py:449; session coordination hard-cut is 65 at sessions/schema.py:40. Adding credential event types changes Landscape CHECK vocabulary and needs a Landscape compatibility sweep, separately from R02's session sweep. Adding a validate-repair origin changes session CHECK vocabulary. The final unpublished candidate can combine compatible schema changes into one planned next epoch per store; if an intermediate candidate is deployed, later incompatible changes need another increment. Never describe a bump as data migration or authorize store destruction implicitly.

## R05 — global session/identity lock order

**Fix:** Audit the complete transaction path, including foreign-key key-share acquisition. Make chargeable admission acquire the session row before the identity. For settlement, decide the complete order **session → provider attempt → identity/ledger** after enumerating transaction callers; do not mechanically add session locking after the existing attempt lock if another caller acquires session then attempt. Preserve session-operation fence validation and timestamp-after-lock semantics. Review review-authority/token-ledger callers for consistency with the chosen order; fix only paths needed to eliminate reachable cycles, documenting any separately proven defect. Update progress comments to state the order actually enforced.

**Files:** `src/elspeth/web/coordination/chargeable_admission_authority.py`, `quota_authority.py`; inspect `sessions/service.py` begin/settle entry points, `websocket_ticket_authority.py`, `composer_progress_authority.py`, `review_authority.py`. Tests: extend `tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py`, `test_cross_process_ticket_postgres.py`, `test_quota_authority_postgres.py`; focused unit quota tests under `tests/unit/web/coordination/test_quota_authority.py`.

**Proof:** Use real PostgreSQL, separate connections/engines and the public SessionService `begin_provider_attempt`, not a replacement helper that prelocks session. SQLAlchemy event hooks/barriers should pause the admission after its identity lock in the old implementation, start a ticket issue/consume or heartbeat request, observe actual blocking via `pg_stat_activity`/`pg_blocking_pids`, then release the admission to its real FK-backed INSERT. Avoid a barrier that deadlocks the fixed code by waiting for a lock acquisition the fix intentionally prevents: gate on query submission and database wait state, not both acquisitions. Parameterize admission versus issue, consume, heartbeat and relevant progress mutations. Reproduce SQLSTATE 40P01 on baseline or use a controlled negative mutation that restores the old order; assert both fixed operations finish within bounded statement/future timeouts. Test actual settlement similarly, including replay/no duplicate ledger charge, disabled/missing owner refusal and rollback. Do not assume which side is the deadlock victim.

**Command:** `pytest tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py tests/testcontainer/web/test_cross_process_ticket_postgres.py tests/testcontainer/web/test_quota_authority_postgres.py -m testcontainer -n 0` (sole PostgreSQL lane). Broader PostgreSQL gate at integration.

## R07 — dev-admin authority must not transfer with a reusable name

**Evidence:** admin_routes.py:131 still grants by username; :139 bypasses the live role query. The profile route duplicates that test at auth/routes.py:795–797. Open admission is activated at app.py:1116–1118.

**Fix:** Remove reusable username as an authority proof. Preserve the credential-only dev grant by binding its one-time bootstrap to an immutable identity id and checking the live active local human identity on every request. Use the existing audited identity/bootstrap machinery where applicable, with an explicit durable consumed binding if needed; do not silently elevate the dev credential grant into deployment-wide identity administration. Reusing a username must never reseed consumed authority, and resolving the configured name to whichever account exists on every request or restart is insufficient. Reserve the configured bootstrap name at self-registration while bootstrapping and give a useful refusal for ordinary admin deletion until replacement behavior is defined. These guards complement durable binding, not substitute for it. Preserve live role-holder access for every local Administrator and preserve external-auth route hiding. Consolidating the development grant into the normal admin role is an alternative only if its authority expansion is explicitly accepted.

**Files:** auth/admin_routes.py, auth/routes.py capability response and registration, auth/local.py, coordination/identity_authority.py bootstrap, app.py wiring, config.py docs only as coordinated. Tests: `tests/unit/web/auth/test_local_account_admin_authorization.py`, `test_admin_routes.py`, `test_routes.py`, `test_local_provider.py`, `tests/unit/web/test_local_auth_wiring.py`, identity authority tests; PostgreSQL `test_identity_last_admin_race_postgres.py` for shared admission/retirement authority.

**Scenarios:** Admin A deletes dev account, anonymous registration reuses the name, fresh identity cannot list/create/reset/delete credentials; repeat after restart and CLI removal; never-created reserved name cannot gain authority; disabled/pending/nonhuman/expired/revoked identities cannot use the special grant; current ordinary local administrator still can; external-auth caller cannot; username/display-name mismatch does not identify the principal. Preserve R5 final-admin safety and credential-generation fence under concurrent reset/re-registration. Any required new durable binding belongs to central epoch integration.

## R08 — audited credential creation/reset across separate stores

**Evidence:** admin_routes.py:203 and :220 only log; local.py:720–735 mutates password hash directly. Existing `register_open_user_with_audit` at local.py:483 onward demonstrates durable intent plus delivery/compensation, not a single transaction across auth.db and Landscape.

**Fix:** Add closed credential-created/password-reset auth event types, explicit actor identity id, target username/identity where present, request id/client host/user agent, outcome and no password/hash. Extend recorder contracts, implementation, schema CHECK parity and epoch. The web mutation must not return a generated password before durable audit succeeds. More importantly, do not make a successful reset unaudited by writing the audit only after the hash update. Design typed durable credential mutation intent with generation fencing and recoverable outcome: commit intent atomically with mutation, deliver audit, acknowledge intent; fail closed on delivery failure and either restore only the exact affected generation or quarantine/reconcile it. Do not invent a cross-store transaction. Reuse registration's mechanism where it preserves reset semantics. Preserve documented JWT lifetime semantics; resetting a password does not automatically promise sign-out.

**Files:** auth/admin_routes.py, local.py, audit.py; contracts/auth.py; core/landscape/schema.py CHECK and schema version, auth_audit_repository.py validation; app.py recovery wiring if needed; auth DB intent schema/version if required. Central schema writer owns epoch edits.

**Tests:** existing auth route/provider/audit lifecycle tests, `tests/unit/contracts/test_auth_audit_event_input.py`, Landscape auth repository/schema parity tests (locate exact existing names before coding). Read actual persisted rows; assert actor A and target B and absence of secret material. Inject failure before credential commit, at audit delivery, during compensation and process-restart recovery; prove no unaudited usable replacement survives and that concurrently replaced credentials are never rolled back. Run auth tests serially; PostgreSQL full gate required because Landscape and identity persistence change, plus forced replacement-race cases adjacent to existing credential fence tests.

## R09 — carry web deletion actor and request into durable events

**Fix:** Pass an owned immutable administrative provenance value through `LocalAuthProvider.delete_user`, `RetireIdentity`, `local_identity_retirer`, `IdentityRetired`, `IdentityAuthorityRevoked` and recorder callbacks. Capture bounded request provenance before thread dispatch; do not pass mutable Request into repository code. Web events name authenticated immutable actor id; CLI records explicit operator context with no invented request. Record actor even when finishing a previous failed retirement; preserve distinction between original mutation and retry where the durable recovery mechanism supports it. Never lose the generation fence while changing the callback signature.

**Files:** auth/admin_routes.py, local.py, auth/audit.py, coordination/identity_authority.py, identity_lifecycle.py, app.py callback, cli.py `_composer_retirement_recorder` and deferred retirer wiring. Update misleading operator-only docstrings. Tests in admin_routes/local_provider/audit_lifecycle/identity_authority plus CLI removal tests and `test_identity_last_admin_race_postgres.py`.

**Proof:** Two admins perform deletions; persisted auth row and authority-revoked event each carry the correct actor and request. CLI event remains operator with null request. Audit failure preserves the fail-closed retirement contract; retry after credential removal is attributed; cross-generation replacement is preserved. No secret values enter audit metadata.

## R59 — document the live authorization input

**Fix:** Correct `identity_authority.py:1471–1482` docstring to explain the unlocked per-request READ COMMITTED live-authority input and separate transaction-protected R5 decision. If R07/R08 introduces a stronger mutation-time authority check, describe that final behavior rather than pinning the old race window. Fold into the same auth writer. No standalone test for prose; use existing authorization tests to support the final contract.

## R12 — audited effective execution rate policy

**Fix:** Fold operator `execution_rate_limit` into effective `settings.rate_limit` both in `_approval_inputs_from_frozen` (service.py:1233–1237) and run setup (:3442–3455), before audit config/hash construction. Build RateLimitRegistry from those exact effective settings (:3629–3636). Prefer one small existing policy-folding helper if it prevents these paths drifting; avoid unrelated refactor.

**Tests:** `tests/unit/web/execution/test_service.py` currently asserts the registry config differs from loaded settings at :4446; update that assumption. Prove nondefault limits appear in persisted Landscape config and govern registry construction; different operator limits produce different approval hashes; approval then policy change refuses before provider call/permit; defaults and disabled limiter behave consistently; resume records the effective resuming policy through its audited contract. Include `tests/unit/web/coordination/test_approval_authority.py` and testcontainer approval authority where affected.

## R51 — reject impossible rate-limit path at configuration admission

**Fix:** Add WebSettings after-validation using `RuntimeRateLimitConfig.from_settings(self.execution_rate_limit, state_dir=self.data_dir)`. Reuse the path validator rather than duplicate URI/traversal rules. Do not open/create a rate-limit DB just to validate config. Keep the per-run check as a boundary against changes after boot.

**Files/tests:** `src/elspeth/web/config.py`, `contracts/config/runtime.py` reused unchanged where possible; `tests/unit/web/test_config.py`, runtime config path tests, `tests/unit/web/execution/test_service.py`. Cases: `../escape`, absolute outside root, URI, blank/space, symlink escape where supported; valid relative/absolute inside root, disabled limiter and no persistence. Verify env JSON parsing and actionable boot error; do not conflate syntactic custody validation with writable-filesystem health.

## R13 — recover cancelled runs with unfinished outputs

**Fix:** Include `cancel_pending` in candidate SQL and locked recheck in run_recovery_authority.py:147/:160. Preserve owner/fence/membership checks and `recovery_required` exclusions. Confirm `RunRecoveryService._reconcile_terminal` maps interrupted audit observation to cancelled and settles outputs idempotently. Update execution/service.py recovery claim only after the path is proved.

**Tests/files:** `tests/unit/web/coordination/test_run_recovery_authority.py`, `tests/unit/web/execution/test_recovery_coordinator.py`, `test_service.py`; `tests/testcontainer/web/test_global_run_recovery_postgres.py`. Exercise real running→cancel request→cancelled with injected output-finalization failure, expire/release owner authority, peer recovers remaining blobs and terminal settlement exactly once. Negative control: live owner excluded; already-settled cancelled run excluded; recovery-required run not silently repaired. Also cover completion racing cancel_pending with finalization failure. Run actual PostgreSQL recovery proof serially.

## R50 — distinguish durable status from durable event

**Fix:** Track successful terminal status transition independently of terminal event persistence. Settle only after durable terminal status, durable terminal event and completed outputs, under current fence. A swallowed failed-status update or signal path must not set that conjunction. Do not catch and hide arbitrary custody errors in finally; avoid calling settlement when its precondition is false. Update :4341–4348 comments.

**Tests:** `tests/unit/web/execution/test_service.py` existing :1016–1128 settlement tests plus :5495 signal and failure-callback tests. Inject original exception then SQLAlchemyError/OSError from failed-status update while failed-event write succeeds; assert original exception remains top-level and settlement not called. Repeat KeyboardInterrupt/SystemExit; valid failure/completion/cancellation with durable status still settles. Pair with R13's peer recovery proof; do not claim the original exception preserved merely from its `__context__`.

**P3 focused command:** `pytest tests/unit/web/execution/test_service.py tests/unit/web/execution/test_recovery_coordinator.py tests/unit/web/coordination/test_run_recovery_authority.py tests/unit/web/test_config.py -n 0`; PostgreSQL recovery/approval tests next, full gates at integration.

## R35 — actor/hash provenance parity

**Evidence:** coordination/repository.py:1228/:1256 and pending_interpretation.py:2089 retain `actor="composer-llm"`; origin is recorded separately at repository.py:1267.

**Fix:** One closed origin→actor mapping used by both hash construction and row writer; keep composer sentinel only for COMPOSER_LLM, use explicit system actor for server origins. Correct automatically synthesized opt-out marker actor separately (no surface origin) without replacing true user opt-out attribution. Update sessions/service.py create-pending docs, contracts/composer_interpretation.py and `src/elspeth/web/frontend/src/types/interpretation.ts` docs. Never rewrite immutable resolved rows. Actor is an existing hash input: new rows must hash the recorded actor; existing row verification must use the recorded value, not rederive actor and invalidate retained evidence. Decide if any hash-domain semantic version changes are necessary after reading the verifier; changing an input value alone does not automatically mean the serialization grammar changed.

**Tests:** `tests/unit/web/sessions/test_routes.py::test_post_state_yaml_opt_out_returns_the_final_durable_head`, `test_interpretation_events_service.py`, `test_interpretation_opt_out_routes.py`, `test_interpretation_events_table.py`, `tests/unit/contracts/test_composer_interpretation.py`. Parameterize pending/opted-out/superseded across composer, import, revert, E2E seed; check null LLM provenance for server origins, actual stored actor, independently recomputed hash and immutability after resolve. A negative mutation changing only stored actor or only hashed actor must fail. PostgreSQL schema/hash persistence coverage is required.

## R36 — truthful validation repair origin

**Fix:** Prefer explicit `VALIDATE_REPAIR` server origin with null model/provider/skill fields for the `/validate` backstop rather than guessing a historical author from current model settings. Make the caller supply origin/owned provenance explicitly so ordinary compose finalization retains COMPOSER_LLM and recorded planner identity. Add closed enum arm, all CHECK/nullability parity, schema epoch and frontend decoder/type arms through central schema owner. A stronger alternative is authoritative state-origin evidence if already durable and sufficient; do not infer it from free-form metadata. The implementation review must ensure both guided and freeform `/validate` arms use the repair origin and no provider is falsely attributed.

**Files:** contracts/composer_interpretation.py, composer/service.py, execution/routes.py, sessions/models.py, relevant schema guards, frontend interpretation type/decoder and tests. Overlap with R03/R14 state-write lane requires serialized ownership.

**Tests:** `tests/unit/web/composer/test_surface_pending_interpretation_reviews.py`, `tests/unit/web/execution/test_routes.py` and `test_routes_interpretation_review_drift.py`, session interpretation table/service/route tests. Revert commits new head; inject failure in postcommit surfacing; validate that head before retrying revert; assert truthful repair origin, all LLM fields null, actor/hash parity, exactly one surface per site. Retry validate and revert in opposite orders without rewriting existing evidence or duplicate cards. Include normal compose finalization control retaining actual provider identity.

## R52 — expected refusal for obsolete signed snapshots

**Fix:** Catch the known snapshot schema-decoding failure at the narrow retained-share parsing boundary and raise `InvalidToken` with safe unsupported-shape reason. Include both `AuditReadinessSnapshot.model_validate_json` and the composition snapshot decoding failures the decoder actually declares. Do not catch all ValueError/Exception across projection/rendering, swallow payload-integrity failures or invent fallback migrations. Update resolve_token's documented error contract.

**Files/tests:** `src/elspeth/web/shareable_reviews/service.py`; `tests/unit/web/shareable_reviews/test_service.py`, `test_routes.py`, `test_models.py`. Build correctly signed/digested old snapshots containing retired `managed_identity_policy`, a blocker missing newly required `note`, and unsupported composition shape; assert 401 and no stack/secret detail. Current snapshot resolves; tampered digest still raises integrity failure; missing/reaped payload stays 404. `pytest tests/unit/web/shareable_reviews -n 0`.

## R11 and R73 — bounded proxy trust and loopback backend

**Fix together:** Bind Compose's host publication to `127.0.0.1:8451:8451`, retain container `0.0.0.0`, and replace vague firewall advice with the supported loopback/TLS topology. Configure Uvicorn forwarded trust for the actual nginx peer as delivered through the deployment network. Do not hardcode a guessed default bridge IP or use unrestricted `*`. Choose a deterministic documented network/gateway or explicit operator-configured trusted CIDR/address with deployment validation. Read the installed Uvicorn ProxyHeadersMiddleware/config and official Docker/Uvicorn docs before implementation; this planning lane did not experimentally measure DNAT behavior. nginx must continue overwriting forwarded headers from clients.

**Files:** deploy/compose/web-postgres.yaml, nginx.conf, docs/guides/docker.md; potentially cli.py/web/config.py if adding first-class forwarded trust settings, coordinated with P2/P3. Tests: `tests/unit/deployment/test_compose_bundle.py`, `tests/unit/web/test_deployment_profiles.py`, `test_deployment_contract.py`, existing rate-limit tests and CLI web launch tests. Add real middleware path test for trusted gateway and distinct XFF clients: user A hitting limit does not throttle B, audit IP equals A/B; untrusted direct peer spoofing XFF cannot choose its rate-limit/audit identity. Test forwarded proto and WebSocket path too. Parse rendered Compose to assert loopback publication and the matching configured trust. Deployment acceptance with host nginx+container must measure the observed peer and prove external backend port refusal; static tests alone cannot certify Docker routing.

## R71 — systemd timeout prose

Keep systemd's 180/240 values unless an independent product change requires alignment. Correct elspeth-web.env.example:17 to say systemd profile limits, :21 to state actual minimum ingress timeout and the shipped Compose nginx's 360s. Existing `tests/unit/deployment/test_linux_systemd_bundle.py` covers intended 180/240 behavior; update any textual assertions that encode the false nginx claim. Do not silently raise timeouts for a documentation bug.

## R72 — effective configuration reference and limiter scope

Update docs/reference/environment-variables.md with Docker 300 timeout + 360 ceiling + 30 headroom; distinguish defaults and per-profile example values. Add EXECUTION_RATE_LIMIT JSON shape, service key mapping and path/root rules after R12/R51 decisions. Correct web/config.py comment to include non-LLM keys. Derive key list from actual plugin limiter calls/config contracts, not guessed provider names: known examples include `azure_ai_search:<hostname>`, `web_scrape`, `blob_fetch`, Dataverse keys. Explicitly explain fresh registry per run, single-process web execution serialization, optional persisted buckets across runs sharing the same storage, and lack of cross-replica coordination on replica-local files. Do not repeat the refuted N-concurrent-runs-in-one-process claim.

**Validation:** `tests/unit/docs/test_deployment_platform_docs.py` already exists; extend meaningful reference/config parity assertions only where useful. Parse example JSON via real WebSettings and render Compose/systemd examples; check the invalid timeout/ceiling combination is rejected and documented valid combinations accepted. Do not add tests for each prose sentence.

## Review and integration gates

Each implementation lane first reproduces its reported behavior against the frozen current base and records whether post-pin changes affect it; an already-fixed disposition needs failing-before/passing-current proof or equivalent exact evidence. Use one author and an independent reviewer per coherent change. Reviewers inspect full touched files, exported DTO/enum/DB CHECK parity, negative controls, and preserved authorization/provider invariants rather than only test output.

All commands above are future selectors, not results. Execute from an explicit worktree root with both worktree Python source roots and an actual worktree-local environment, write unique lane logs, record exit code after completion. Unit selectors here use `-n 0`; PostgreSQL capacity is centrally serialized. After local lane proof, integrate in dependency order: prepared credential fence reconciliation → credential changes; R35 mapping → R36 enum; R12/R51 policy → R72 docs; R11 trust+R73 loopback together; R13 recovery+R50 terminal flags together. Schema owner stages one consistent candidate epoch sweep after all dependent shapes stabilize.

Because these changes touch shared persistence, contracts and runtime execution, final combined integration needs the canonical whole-tree gates and full default suite plus serial full PostgreSQL testcontainer selection on a frozen candidate. Compare trust-tier findings/binding drift to frozen base without signing or globally clearing deliberate failures. Independent reviewer verifies R07 exploit refusal, R08/R09 real audit rows, R05 actual concurrency and R13 peer recovery before recommending merge. Local tests do not certify actual proxy deployment behavior; reserve explicit container/nginx acceptance evidence and any operator deployment/store recreation for their authorized boundary.
