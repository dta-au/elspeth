# Shared graph links: investigation and 0.8.7 remediation

Status: local 0.8.7 candidate implemented and independently accepted for the bounded fixes. Integration and deployed acceptance remain pending; no deployed incident cause established.

## Baselines and scope

- Task `01a13f6f-d396-7c61-b8c1-14eee5b07dc9`, host `nyx.foundryside.dev`, isolated worktree, startup clean and detached at main `9d7735c094eab212549868afeaf6019272cf5c57`. App creation receipt confirms selected investigator Sol 6.1, xhigh.
- Authoritative GitHub `refs/heads/main` was read through the connector and still names that SHA.
- Separate candidate comparison: `02e6b893f35230db544c1f277988c61b89203a54` (.6); streaming candidate `1dec423d306513f28325573cd07e164196cb5686`; PR #281 merge `cb6b40956e8544f89d2b2143ebf82cf1af106b85`; its first parent `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`.
- The previous candidate report `docs/reviews/shared-link-visibility-investigation-20261015.md` was a static infrastructure review. Its “no obvious defect” result did not exercise recipient-workspace substitution.
- Scope expanded by John during this task to validated fixes for 0.8.7. This owner implements only in its isolated worktree; the existing .7 integrator owns application/integration. No deployment, paid calls, credentials, settings, live data, or other owner worktree changes.
- Direct authorization subsequently confirmed: “Proceed with the shared-link fixes for .7, with tests and independent review”. Local branch: `codex/shared-graph-links-087-20261015`; integration owner remains chat `01a13f16-b2fe-7d31-bd40-0b72c028e00c`.

## Findings

### P2: malformed shared fragments display the recipient's normal workspace

The parser returns the same `null` for “not a shared route”, an empty token, and bad percent encoding. App interprets that value as permission to render normal Composer. The regular router separately treats the shared prefix as non-routing, so it leaves the selected session and fragment alone. With Bob's graph already selected, `#/shared/` and `#/shared/abc%ZZ` therefore render Bob's workspace rather than a share-link refusal. A cold malformed arrival with no selected graph produces an ordinary empty workspace; it does not conjure Bob's graph.

Pinned evidence: [parser lines 23–31](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/hooks/useSharedToken.ts#L23-L31), [App gates lines 173–193](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/App.tsx#L173-L193), [App branch lines 696–708](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/App.tsx#L696-L708), [router's shared-prefix guard](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/hooks/useHashRouter.ts#L62-L70).

Executed reproduction: real App with the repository's existing child stubs and Bob's selected state mounts `composer-workspace-stub`, preserves Bob's graph state and the malformed fragment, and mounts neither shared loading nor error. Separate production-hook/store tests reproduce the same choice. This establishes incorrect view selection; heavy graph rendering is stubbed in the App harness.

This is a demonstrated integrity/UX defect. It does not demonstrate cross-user access, transmission of Bob's graph to Alice, or exploitation of session ownership. It does not establish that the reported link was malformed.

### P2: an unavailable ordinary session URL can silently auto-resume the recipient's graph

An ordinary `#/{sessionId}` link is private and deliberately does not grant sharing access. When Alice's session fetch receives 404, `selectSession` clears the binding and the hash subscription removes the fragment. If the recipient's session list/preferences are not ready until after that, auto-resume first sees an empty hash and chooses Bob's newest session. If auto-resume inspects the original fragment earlier, its `attempted` latch prevents that choice. This is an initialization-order defect, independent of the correct backend ownership refusal.

Pinned evidence: [private ownership refusal](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/sessions/ownership.py#L26-L50), [selection and 404 cleanup](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/stores/sessionStore.ts#L1044-L1116), [hash echo/removal](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/hooks/useHashRouter.ts#L297-L317), [auto-resume](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/hooks/useAutoResumeSession.ts#L22-L55).

Executed reproduction uses the real production selection method for Alice, four mocked 404 HTTP readers, then a delayed Bob session-list publication. The observed selection order is `[Alice session, Bob session]`, and the final fragment names Bob's session. Bob's successful hydration is synthetic. No network/provider request is involved.

This distinguishes correct refusal of a private URL from misleading substitution after refusal. It does not justify relaxing ownership checks. A root URL with no fragment legitimately auto-resumes the recipient's workspace; copying the address bar is not a Share for review grant.

## Generated link, authentication and shared-fetch trace

The sender's Share for review dialog copies `window.location.origin + share_url`; the server emits `/#/shared/{signedToken}`. The token is opaque and does not contain a recipient-selected session identifier. [Copy URL construction and clipboard call](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.tsx#L31-L37), [server mint](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/shareable_reviews/service.py#L530-L544).

For a recognized valid token, App suppresses normal hash routing and auto-resume and renders SharedInspectView within AuthGuard. AuthGuard stores/restores the fragment in sessionStorage around authentication. SSO callback capture synchronously scrubs the handoff fragment; the saved share destination survives that scrub. Offline auth-transition/remount tests exercised local login and this SSO route restoration. They simulate authentication state and do not exercise a real identity provider. [AuthGuard](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/common/AuthGuard.tsx#L40-L71), [callback scrub](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/auth/ssoCallback.ts#L115-L121).

The client fetches `/api/sessions/shared/{encodedToken}` with authentication. That endpoint requires an authenticated identity and validates the capability rather than session ownership. It resolves a signed, content-addressed frozen snapshot; it does not look up the recipient's active graph. The renderer gives GraphMiniView the snapshot override, which takes precedence over the live store. [Client](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/api/shareableReviews.ts#L204-L217), [route](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/shareable_reviews/routes.py#L151-L183), [snapshot resolution](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/shareable_reviews/service.py#L598-L649), [override](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/sidebar/GraphMiniView.tsx#L35-L53).

The shared view treats 401 and 404 as explicit errors and offers an explicit Return to my workspace link; it does not automatically navigate there. The token-specific fetch is aborted on effect cleanup and late resolutions are ignored. A successful snapshot rendered Alice's two-transform graph while Bob's three-transform graph remained selected in the store. Inspection does not copy/edit the sender's session, and shared GraphMiniView has no mounted GraphModal receiver. [Error/loading lifecycle](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx#L146-L208), [read-only rendering](https://github.com/dta-au/elspeth/blob/9d7735c094eab212549868afeaf6019272cf5c57/src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx#L255-L330).

Streaming observer/custody publication changes the session store, not this token's snapshot fetch or route. A late active-session publication did not change a valid share fragment in the offline hook test. This bounds the exercised race; it is not a real stream/reconnect or browser acceptance run.

## History and separate .6 comparison

The parser, normal hash router, auto-resume hook, AuthGuard, share-copy dialog and SharedInspectView have identical SHA-256 file bytes at PR #281's first parent, the initial streaming candidate, the streaming merge, pinned main and pinned .6. The parser's null-on-malformed lines originate at `68512356f3` (May 19); auto-resume predates streaming. The pre-stream store already contains the same current-selection-guarded 404 cleanup.

All six synthetic production-hook/store scenarios also passed against a task-owned `git archive` of pre-stream `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. Both bad behaviors are therefore reproduced on each side of the streaming merge. This excludes that merge as their introduction; it does not identify the reported incident's exact regression range or exclude different deployment/streaming defects.

Pinned .6 changes sessionStore elsewhere; none of the six listed routing/share files changed. No .6 product mutation or test execution was performed. The .6 whole application was not tested by this owner.

## Implemented correction

1. `utils/hashRoutes.ts` separates shared-route ownership (`none`, `invalid`, `valid`) from token parsing and owns shared/private/auth-callback classification. `useSharedRoute` replaces the internal token-only hook. Bare, empty and badly encoded shared fragments stay on an authenticated shared error surface, cannot expose normal Composer, and make no shared API request. App keys the shared child by route identity; valid token decoding and capability/authentication checks are preserved.
2. `useAutoResumeSession` captures explicit arrival intent during render and capture-phase hash/popstate events, before readiness or normal-router writes. A refused link cannot trigger automatic selection of another session. `useHashRouter` supplies a generic unavailable-link notice above the tutorial/empty branches; it does not reveal session existence or ownership and ignores stale refusal after another selection.
3. AuthGuard persists ordinary private destinations as well as shared destinations through its existing sessionStorage key. The resume latch reads that validated pending destination before callback restoration consumes it. A genuine root SSO callback remains eligible to resume. Explicit current navigation wins over stale storage; navigation away disarms the pending destination. This also closes the reproduced stale shared A/B destination issue while avoiding the same substitution for newly persisted private links.
4. Permanent regressions cover malformed routes/no fetch, current versus saved destination, auth remount, callback/root compatibility, capture-phase arrival, both list-first/refusal-first orders through the production selection method, and stale A after explicit B. Existing share rendering, clipboard and error controls remain green.

These are frontend behavior changes. They require no persisted-data migration, backend permission change, provider bypass, or new tutorial-specific path. The hook is internal, with its sole production caller updated. Generated public share URLs, decoded token handling, the existing auth storage key and private-session authorization remain compatible. Destination precedence intentionally changes from first-saved-wins to current explicit user navigation; private destinations now survive authentication rather than becoming root arrivals.

The auth-remount extension was required for completeness: an in-memory latch alone cannot survive a fresh SSO callback page. Two failing-first controls proved that the private destination was not saved and that the fresh page selected `recent` before restoration. The capture-phase listener likewise has a failing-first control demonstrating a hash entered and erased before any bootstrap dependency changed.

## Independent reviews and remaining related finding

The actual selected correctness/security reviewer was `gpt-6-astra`, reasoning `xhigh`, dispatched with no inherited history as `/root/astra_shared_links`. Its final verdict is **no blocking findings in the final bounded candidate**. It independently read immutable history, full changed production files and the completed offline receipts; it ran no tests. The selected investigator was Sol 6.1/xhigh as recorded by the app creation receipt. A separate inherited-model systems reviewer (`/root/systems_shared_links`) also accepted the bounded core patch with **no unresolved core findings** after identifying the same-document and SSO-remount gaps now corrected. Its exact model routing was not independently exposed, so no model identity is inferred for it.

Both reviewer reports remain in the task-owned ignored evidence directory as `astra-review.md` and `systems-review.md`; their conclusions and limits are recorded here without adding review sidecar packages. Acceptance applies to the local frontend candidate, not integration, CI, a release, or production recovery.

**P2, related and outside this patch:** Composer keyboard handlers remain mounted during shared inspection. A synthetic real-App control demonstrated Ctrl/Cmd+N invokes recipient `createSession` while the shared view is mounted (`related-red.log`, one assertion failure). Ctrl+Shift+V can also request recipient validation according to source, but that shortcut was not separately reproduced. This does not copy/edit the sender's frozen snapshot or establish cross-user access. No claim that shared inspection performs zero recipient-side actions is made. The other two failures in that exploratory log reproduced stale shared A/B destination precedence; those are now corrected by the unified authentication destination rule and passing permanent regressions.

## Execution evidence and limitations

Task-owned fixtures/logs/config are under the ignored `.claude/lanes/shared-links-20261015/` directory. Existing dependencies are referenced read-only through worktree-local symlinks; Vitest cache lives in the task directory. Node `v24.13.0`, Vitest `4.1.11`; the runner root and source alias point into the pinned worktree. Python import provenance was verified for both `elspeth` and `elspeth_lints` in this worktree with explicit dual-root PYTHONPATH.

Host capacity was read through an approved read-only process inventory, using positive Python/Vitest and negative shell controls. Four Keisei serial pytest masters were present; its pyproject declares no xdist default. No ELSPETH suite was present. Each focused run uses one worker, sequentially, within the shared ceiling of 12. No broad suite/native proof was launched and no owner was instructed to change their work.

Commands (run from the relevant explicit worktree/frontend directory):

```bash
node src/elspeth/web/frontend/node_modules/vitest/vitest.mjs run \
  --config .claude/lanes/shared-links-20261015/vitest.config.ts \
  --testNamePattern 'INVESTIGATION|App shared-route'

# From src/elspeth/web/frontend, same config, select the six baseline hook/view/dialog files.
node node_modules/vitest/vitest.mjs run --config <task-config> <six-selected-test-files>

SHARED_PROBE_SOURCE=<task-owned-pre-stream-archive>/src/elspeth/web/frontend/src \
  node src/elspeth/web/frontend/node_modules/vitest/vitest.mjs run \
  --config .claude/lanes/shared-links-20261015/vitest.config.ts routes.probe.test.tsx
```

Raw completed summaries:

```text
probe2.exit: 0
Test Files  4 passed | 6 skipped (10)
Tests       13 passed | 169 skipped (182)

baseline-frontend2.exit: 0
Test Files  6 passed (6)
Tests       93 passed (93)

pre-stream-probe.exit: 0
Test Files  1 passed (1)
Tests       6 passed (6)
```

Initial harness runs failed because the task config omitted React's transform, and because a baseline CSS test requires frontend cwd; both were corrected before the completed results above. Those failures are retained in `probe.log` and `baseline-frontend.log`, not product findings. An initial remediation test invocation rejected an unsupported CLI `--include`; no test ran in that invocation. An initial same-batch regression lacked an `act` import, and initial TypeScript checking found two test-fixture type errors; these were corrected without suppressions before the results below.

Final completed results, each with its own checked exit file and complete log:

```text
fix-red2.exit: 1              4 expected assertion failures before correction
notice-red.exit: 1            missing unavailable-link notice before correction
arrival-race-red2.exit: 1      unintended recent selection before capture listener
private-sso-red.exit: 1       2 expected assertion failures before auth persistence
final-frontend.exit: 0        8 files, 182 tests passed
typecheck2.exit: 0            no TypeScript diagnostics
eslint2.exit: 0               no ESLint diagnostics
backend-controls.exit: 0      7 tests passed, 1 upstream Starlette/httpx deprecation warning
pre-stream-recheck.exit: 0    6 historical production-hook/store reproductions passed
```

Final frontend command (from the explicit worktree's `src/elspeth/web/frontend` directory):

```bash
node node_modules/vitest/vitest.mjs run --config <worktree>/.claude/lanes/shared-links-20261015/vitest.config.ts \
  src/App.test.tsx src/utils/hashRoutes.test.ts src/hooks/useSharedRoute.test.ts \
  src/hooks/useHashRouter.test.ts src/hooks/useAutoResumeSession.test.ts \
  src/components/common/AuthGuard.test.tsx src/components/shared/SharedInspectView.test.tsx \
  src/components/composer/SaveForReviewDialog.test.tsx
node node_modules/typescript/bin/tsc -p tsconfig.app.json --noEmit
```

The serial backend command used the main checkout's existing interpreter, both worktree source roots in `PYTHONPATH`, explicit `-o pythonpath='src elspeth-lints/src'`, `-n 0`, and these exact selections from `tests/integration/web/test_shareable_reviews_routes.py`: `test_mark_ready_for_review_happy_path`, `test_mark_ready_for_review_idor_returns_404`, `test_get_shared_inspect_recipient_is_not_creator`, `test_get_shared_inspect_requires_auth`, `test_get_shared_inspect_tampered_token_returns_401`, `test_get_shared_inspect_expired_token_returns_401`, `test_get_shared_inspect_blob_expired_returns_404`. These use temporary SQLite/payload fixtures and synthetic identities; authentication dependencies are overridden. Positive Alice/Bob sharing and negative capability/ownership controls passed. No live provider or identity-provider call was made.

The final pre-stream recheck still passes with the old hook now removed from the working candidate, corroborating that its alias executes the archived production hook/store rather than current source. It retains the task setup and synthetic surface, not a complete historical app build.

Pending boundary: integration owner application and its existing integration/CI/release gates. Full frontend, full Python, PostgreSQL and native proofs were not run: changed production code is confined to frontend routing and does not change a schema, persistence, lock or backend authorization. Real browser navigation, deployment revision/configuration, recipient's actual link shape, real streaming reconnect and identity-provider behavior remain unverified. No production data was inspected. No final deployment or incident-cause conclusion is warranted without the affected link form and deployed revision. Authoritative GitHub main was rechecked after final execution and remained at the original pin.
