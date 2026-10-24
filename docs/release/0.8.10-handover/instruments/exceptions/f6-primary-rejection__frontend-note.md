F6 frontend evidence

Files: frontend src/api/client.ts, src/types/index.ts, src/stores/sessionStore.ts,
src/stores/executionStore.ts, new src/stores/requestRejection.ts,
src/api/client.provider-rejection.test.ts, src/stores/primaryRejection.test.ts.
Shared ApiError decoder uses the existing strict SafeRequestRejection admission;
malformed present descriptors fail decoding. Historical absence stays absent.
Both stores select fixed primary-specific copy and retain an existing diagnostic
reference. Composer's debug appendix remains separate. No Advisor copy is used.

Test identity before repairs: HEAD fe92784450918d66f3faf9560595609e28812289.
Baseline source SHA256: client.ts d9e114a7a82a785668a0c58d63f26223d98a6b863ba2df752ec8029339feeb9d;
sessionStore.ts 3065953098e5ed5b1a002dcacb06f6ad54f7baa1241c9063cf440a09d32ddf91;
executionStore.ts d8bd864ee9867c898f4381837035ee50e0fc545e62693dd82cb701352e9f8aba.
Only ApiError's optional field declaration preceded the initial red.

New test ids (describe/test):
- primary request rejection API admission / retains a closed reason through HTTP and durable error decoding
- primary request rejection API admission / keeps historical absence and refuses malformed present descriptors on both decoders
- primary request rejection stores / renders retained durable Composer reason independently of debug=false|true
- primary request rejection stores / renders synchronous diagnostics reason independently of debug=false|true and permits a later successful explanation

frontend-red.log, exit 1, all six fail on assertions (after fixture corrections;
no unhandled errors): retained decoder assertion shows expected request_rejection
missing from received {status:502,error_type:"llm_unavailable",...}; malformed
descriptor assertion is `AssertionError: expected [Function] to throw an error`;
Composer assertion received `The AI service is temporarily unavailable. Please
try again in a moment.` instead of `The Composer request was rejected. The
request exceeded the model's context limit. Reduce the request context before
trying again.` Diagnostics assertion received `The request failed.` instead of
the corresponding `The run diagnostics explanation request was rejected...`.

After decoder prerequisite, frontend-dependent-red.log exit 1: API tests 2 pass,
store tests 4 fail with the same safe-copy assertions, proving rendering defects
remain after the descriptor reaches each store. Client SHA256 after prerequisite:
2aa4f3a747182e4f20f84d9a3c03c468216564f47e8355625c8f7c149fb37d93.
Initial frontend-green.log exit 0: 6 passed.

Named mutation controls, each actual vitest process exit 1, then reverted:
- frontend-mutation-drop-shared-decode.log: remove ApiError descriptor projection;
  6 assertions fail, including retained/http and both stores.
- frontend-mutation-drop-durable-decode.log: apiErrorFromBody clears descriptor;
  4 assertions fail, including durable retained and both Composer debug cases.
- frontend-mutation-drop-composer-rendering.log: safe branch returns generic copy;
  both Composer debug variants fail, diagnostics neighbor passes.
- frontend-mutation-drop-diagnostics-rendering.log: diagnostics reads detail;
  both diagnostics variants fail, Composer neighbor passes.
- frontend-mutation-provider-detail-as-reason.log: Composer uses provider_detail;
  both Composer variants fail (undefined/debug diagnostic instead of safe text).
- frontend-mutation-diagnostics-provider-detail-as-reason.log: diagnostics uses
  provider_detail; both diagnostics variants fail, Composer neighbor passes.
- frontend-mutation-invent-historical-absence.log: fabricate absent descriptor;
  historical `not.toHaveProperty("request_rejection")` assertion fails.
- frontend-mutation-loose-descriptor.log: remove strict guard; malformed-present
  descriptor `toThrow` assertion fails.

Focused compatibility run frontend-focused.log exit 0: 25 passed across the two
new files plus validationReadiness.test.ts, client.execution-errors.test.ts,
client.composerTransport.test.ts. npx eslint on all seven touched TS files exit 0
(frontend-eslint.log); npm run typecheck exit 0 (frontend-typecheck.log).
Actual backend-produced bridge fixture now consumed in primaryRejection.test.ts:
src/test/fixtures/f6-primary-rejections.json (backend agent owns production of it).
Composer body was persisted/reloaded by the real durable route; diagnostics body
was returned by the real synchronous HTTP route, each with debug off/on. Frontend
tests feed these bodies through actual snapshot decoder, observer, shared error
decoder and each store. frontend-bridge-focused.log exit 0: 25 passed, all five
focused files. Final eslint and typecheck both exit 0.

Final bridge fixture mutations also verified/reverted, each exit 1:
frontend-mutation-bridge-composer-rendering.log (2 Composer variants fail),
frontend-mutation-bridge-diagnostics-rendering.log (2 diagnostics variants fail),
frontend-mutation-bridge-debug-prose.log (all 4 store variants fail).
frontend-mutation-open-descriptor-fields.log removes only the exact-field-count
check; the malformed descriptor control fails once the extra field is admitted.
The existing validator is restored byte-for-byte (git diff shows no change).

No frontend pin/baseline expected to move. No commit, docs, or broad suite run.

All remaining admitted copy choices (unsupported_parameter, invalid_request,
absent, unrecognized, unavailable) and an existing valid provider-failure
reference are now neighbors inside the same four store regression cases,
passing through real admission and store rendering. frontend-reason-neighbors.log
exit 0: 6 passed. frontend-mutation-final-renderers.log omits both store safe
renderers; all four store cases fail (exit 1); mutations restored.
