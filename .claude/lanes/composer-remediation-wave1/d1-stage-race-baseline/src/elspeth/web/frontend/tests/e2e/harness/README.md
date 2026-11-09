# Freeform tutorial reliability harness

`../tutorial-reliability.staging.spec.ts` drives the real first-run tutorial on
a deployed build with a live provider: Welcome → freeform Build → explicit Run
→ evidence-backed Audit → Graduation. It resets the configured staging account
between runs and writes one `RunRecord` per attempt under the ignored
`tests/e2e/.harness-results/<batch_id>/` directory.

The route-mocked browser contract in `../tutorial.spec.ts` runs without
provider spend. The live spec and `../helpers/transition-ledger-recorder.ts`
read durable Composer audit rows after each freeform `/messages` response. A
response that publishes a new state or proposal with zero planner provider
calls attributed to **that transition** fails, even if another transition
paid for a provider call. Missing audit evidence and unattributed final rows
fail closed. `transition-ledger.ts` and its unit test pin this invariant.

Run the live battery from `src/elspeth/web/frontend`:

```bash
HARNESS_BATCH_ID=<batch-id> HARNESS_BATCH_SIZE=1 \
STAGING_BASE_URL=<deployed-url> PLAYWRIGHT_BACKEND_BASE_URL=<deployed-url> \
STAGING_USERNAME=<user> STAGING_PASSWORD=<password> \
npx playwright test --config=playwright.staging.config.ts tutorial-reliability.staging.spec.ts
```

The staging config runs sequentially with no retries. Set credentials outside
the repository. Inspect each run's `transitions`, `steps`, run output,
interpretation events, and `landscape` fields before treating a green browser
step as a successful tutorial. `prompt-and-rubric.ts` contains the assumptions
and output-quality rubrics for the current fixed freeform brief; update them
if that brief changes.

Aggregate a completed batch with:

```bash
node tests/e2e/harness/aggregate.mjs <batch-id>
```
