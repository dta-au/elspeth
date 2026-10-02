---
title: Composer tests leak async retry tasks between cases, corrupting asyncio.sleep assertions
labels: [area/tests, area/composer, type/bug, needs-triage]
---

Retry tasks started by one Composer service test outlive the case that created them and are
still awaiting a patched `asyncio.sleep` when a later test asserts on that patch. Retry
assertions can therefore pass or fail according to test order rather than behaviour.

## Background

`tests/unit/web/composer/test_service.py` covers the Composer's LLM call loop, including its
retry-on-transient-failure path. That path's only observable side effects are the provider call
count and an awaited backoff sleep, so the tests assert on both by patching
`elspeth.web.composer.service.asyncio.sleep`. The suite runs in parallel by default under
pytest-xdist, which distributes cases across worker processes.

## What happens

Two tests assert directly on that patched sleep:
`test_litellm_api_error_is_retried_before_unavailable` asserts `mock_sleep.assert_awaited_once()`,
and `test_bad_request_llm_error_is_not_retried` asserts `mock_sleep.assert_not_awaited()` as the
negative half of the no-retry contract — the assertion that a deterministic 400-class provider
error consumes no retry budget. Sleep activity arriving from an earlier case makes either
assertion report something other than the behaviour under test. Because the distribution of
cases across workers varies between runs, the result is order-sensitive: the same code can pass
one run and fail the next.

## Why

Not established. The report records the observation and the work required — diagnose the
leaking predecessor, make retry-task and event-loop ownership explicit, and cancel and await
outstanding tasks at teardown — but it does not name the test that leaks the task, nor the code
path that leaves it running. Treat the mechanism below as the shape of the problem, not a
diagnosis.

## Where to start

`tests/unit/web/composer/test_service.py`, and the retry loop it exercises in
`src/elspeth/web/composer/service.py`. The two assertions named above are the observable
symptom; the leak is upstream of them, in whichever earlier case starts a retry that is never
awaited or cancelled.

**Size.** The diagnosis is most of the work and the repair is likely small once the leaking
case is identified. It suits someone willing to run the file's cases in different orders and
under `-p no:randomly`-style controls to isolate the predecessor; it does not need any product
decision, and it touches test infrastructure rather than shipped behaviour.

## Impact

Contained to the test suite — no user-facing behaviour is affected — but it undermines the
tests that pin the retry policy, which is exactly where a silent regression would otherwise be
caught. An order-sensitive assertion is also a recurring source of unexplained red builds that
cost time to attribute.

## Fix

- Identify the predecessor whose retry task survives its own case, and say in the change what
  it was; the diagnosis is the substance here.
- Make retry-task and event-loop ownership explicit in the Composer service test fixtures, and
  cancel and await outstanding tasks at teardown, so no case can hand work to the next.
- Add an order-sensitive regression proving there is no cross-test sleep activity.

You would know it holds when the two assertions above give the same result under a forced
adverse ordering — the suspect case immediately before each of them — as they do in isolation,
and when the new regression goes red if the teardown cancellation is removed. A repair that
only makes the current ordering pass has not been shown to fix anything.

## Note

The original report located the observation at `tests/unit/web/composer/test_service.py:4356`.
At the current tip that line sits inside an unrelated advisor-failure test, so the case it
referred to cannot be recovered from the line number. The `service.asyncio.sleep` assertions
named above are the surfaces the defect corrupts, not necessarily the case that was observed.
