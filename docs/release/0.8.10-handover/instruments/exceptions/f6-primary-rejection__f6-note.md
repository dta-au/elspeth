F6 — safe primary rejection evidence on both real routes

Base: codex/0.8.10-exceptions at fe92784450918d66f3faf9560595609e28812289.
Changes are uncommitted for the reviewer; no pins or baselines were edited.

Files changed:
- Backend: src/elspeth/web/composer/provider_gateway.py; src/elspeth/web/sessions/routes/_helpers.py.
- Frontend: src/elspeth/web/frontend/src/api/client.ts; src/types/index.ts;
  src/stores/sessionStore.ts; src/stores/executionStore.ts; new src/stores/requestRejection.ts
  (the latter paths are relative to the frontend root).
- Tests: tests/unit/web/composer/test_primary_rejection_gateway.py;
  tests/unit/web/composer/test_primary_rejection_routes.py; frontend
  src/api/client.provider-rejection.test.ts; src/stores/primaryRejection.test.ts;
  src/test/fixtures/f6-primary-rejections.json.

Both gateway arms classify the original genuine LiteLLM rejection and retain its
exact cause, private detail/status, and nonretryability. HTTP projection retains
the closed descriptor independently of debug. Shared ApiError admission rejects
malformed descriptors, leaves historical absence absent, and both stores render
fixed contextual copy, including supplied diagnostic references. The shared
fixture contains actual backend HTTP bodies, with durable persistence/reload for
Composer, and is checked by both backend and frontend tests. Required tool audit,
diagnostics compose-lock ordering, atomic authority persistence and transactional
run/state reproof are covered without changing those owners.

Test ids and actual reds (all red processes exited 1 before the relevant repair):
- test_primary_gateway_retains_rejection_and_exact_cause[tool-primary,text-primary]:
  AssertionError: primary gateway dropped safe request rejection evidence;
  assert None is not None. Only the optional field declaration preceded this red.
- test_tool_primary_durable_route_retains_rejection_after_required_audit[debug-off,debug-on]
  and test_text_primary_diagnostics_route_retains_rejection_and_audit_authority[debug-off,debug-on]:
  AssertionError: HTTP projection lost safe primary rejection evidence;
  assert 'request_rejection' in detail. Gateway prerequisite was repaired first;
  actual dispatch, required audit, HTTP 502 and route category assertions ran first.
- Frontend API admission / retains a closed reason through HTTP and durable error decoding:
  expected request_rejection missing from decoded ApiError. Preservation/validation
  id "keeps historical absence and refuses malformed present descriptors on both decoders"
  also caught malformed descriptors: expected [Function] to throw an error.
- Frontend stores / renders retained durable Composer reason independently of debug=false|true:
  received "The AI service is temporarily unavailable. Please try again in a moment."
  instead of the primary rejection/context-limit copy.
- Frontend stores / renders synchronous diagnostics reason independently of debug=false|true
  and permits a later successful explanation: received "The request failed."
  instead of the diagnostics rejection/context-limit copy. After decoder repair,
  the four store cases remained assertion-red before rendering repair.
Source identities and full assertion output: gateway-red.source/gateway-red.log,
routes-red-identity.txt/routes-red.log, frontend-note.md/frontend-red.log and
frontend-dependent-red.log in this directory.

Mutation controls: every named mutation exited 1 and was reverted.
- Omit tool assignment: only tool case red; omit text assignment: only text red.
- Omit HTTP descriptor: all four routes red. Project after debug-off return:
  both debug-off routes red, both debug-on neighbors pass.
- Drop shared or durable decoding; omit either store renderer; substitute
  provider_detail for safe reason in either store: relevant frontend cases red.
- Synthesize historical absent evidence or loosen closed descriptor parsing:
  historical/validation case red, including a separate extra-field-only mutation.
- Bypass diagnostics audit persistence: both diagnostics cases red on missing
  RunDiagnosticsAuditAuthority persistence; no audit mutation remains.
- P byte-boundary test test_litellm_buffered_response_byte_boundary_and_parser_neighbors:
  <= to < fails "buffered 65536-byte reason was lost"; enlarged limit fails
  "oversized response supplied a reason"; reading the stream fails
  "classifier consumed an unread response stream". This was already green on
  the unchanged parser: no pre-existing parser defect is claimed. Genuine
  LiteLLM body/code absence and exact lengths are asserted, with below-limit,
  malformed/duplicate JSON, invalid UTF-8, unknown/conflicting and unread neighbors.

Final focused checks, actual exits read:
- Gateway/boundary + existing classifier + service nonretry/recoverable neighbor:
  34 passed, exit 0 (gateway-final.log/.exit).
- Both actual backend routes, debug off/on: 4 passed, exit 0
  (routes-final-green.log/.exit).
- Five focused frontend files: 25 passed, exit 0 (frontend-final.log/.exit).
  Existing store cases also exercise every other admitted reason/disposition,
  valid diagnostic references, historical fallback and later diagnostics success.
- Ruff check and format --check on all four touched Python files: exit 0.
- ESLint on all seven touched TS files and npm run typecheck: exit 0
  (eslint-final.log/.exit and typecheck-final.log/.exit).
- git diff --check: exit 0. Classifier and both audit-owner sources are restored.

Independent read-only review found no blocking defects in the complete diff,
new tests, bridge fixture and mutation evidence (review-note.md). It ran no tests.

Pins believed affected: the existing R1/R6 _litellm_error_detail scope bindings
in config/cicd/enforce_tier_model/web.yaml have a changed function scope.
Signing remains suspended; no signing or baseline regeneration was attempted.
No Composer-turn or run-diagnostics audit writer pin movement is expected:
composer_turn.py and execution/routes.py are unchanged. No new dynamic probes,
soft mapping annotations, plugin sources or wire templates. Reviewer must
re-derive whole-tree pins; focused checks do not qualify those gates.

Limits: no commit (read-only .git), full suite, testcontainer suite, forbidden
Landscape architecture test, whole-tree gates, signing, docs files or provider
network calls. The two approved xfails were not edited.

Repository hygiene: the session-start work-at-risk report completed (exit 1,
existing shared-worktree warnings; no state changed). The handover rerun was
bounded to 60 seconds on this loaded host and timed out (exit 124); it is not
a complete fresh shared-worktree census. Both outputs remain in this directory.
