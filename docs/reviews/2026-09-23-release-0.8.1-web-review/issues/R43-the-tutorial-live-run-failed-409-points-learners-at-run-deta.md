# R43. The `tutorial_live_run_failed` 409 points learners at run details the tutorial error screen does not offer

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Pricing, planner failures and tutorial run |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-01-wire#6 |

## Finding

- **Location:** `src/elspeth/web/composer/tutorial_service.py:445-458` and `frontend/src/components/tutorial/TutorialTurn4Run.tsx:365-380`.
- **Wrong:** 101824e7d returns `run_id` and the `rows_*` counts with "Open the run details…". `parseResponse` drops those fields, and the screen shows only Retry and Back. Retry repeats the same outcome.
- **Fix:** Decode `run_id` and link to the run, or change the copy.
- **Sources:** seam-01-wire#6.


## Source findings and verification

### seam-01-wire#6: tutorial_live_run_failed 409 tells users to open run details the tutorial error screen does not offer; run_id is dropped

- **Reported at:** `src/elspeth/web/composer/tutorial_service.py:445`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** 101824e7d added a 409 carrying run_id, the rows_* counts and the copy 'Open the run details to review row errors and source results'. parseResponse keeps only known ApiError fields, so run_id and rows_* are dropped. TutorialTurn4Run renders only the detail text with Retry and Back buttons.
- **Failure scenario:** A tutorial run finishes as empty or completed_with_failures with zero rows succeeded. The learner is told to open run details, but no link or affordance exists on that screen. Retry re-runs the same pipeline and hits the same outcome.
- **Evidence:** tutorial_service.py:445-458. client.ts parseResponse builds ApiError without run_id. TutorialTurn4Run.tsx:365-380.
- **Suggested fix:** Either decode run_id and offer a link to the run, or change the copy so it does not point to an affordance that is not there.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed this at the pinned commit 74c0ce0db. The only change to tutorial_service.py in the window is 101824e7d. It added the 409 branch. When a run ends as `completed_with_failures` or `empty` with rows_succeeded == 0, that branch returns error_type `tutorial_live_run_failed` together with run_id, the rows_* counts and the copy "Open the run details to review row errors and source results." Nothing earlier blocks this path. It is not a cancelled run (handled at line 420), the status is an allowed completion status (line 430), and a landscape_run_id is present (line 439). A run in which every row fails, for example because every LLM call errors, reaches it. On the frontend, TutorialTurn4Run's catch handler only special-cases `tutorial_run_cancelled`. Everything else goes through formatError, which returns the bare `detail` string, and the component renders that string with just Retry and Back buttons. The frontend does not read run_id or rows_* anywhere on this path; the only runId it reads is from a successful response (line 181). So the screen tells the learner to open run details it gives them no way to reach. One point softens this but does not refute it: the learner could still leave the tutorial and look at the session's runs elsewhere, because tutorial sessions are not hidden. Retry clears the cache and runs the same pipeline again. Severity stays low: the copy is misleading and run_id is dropped, but the learner is not blocked.
