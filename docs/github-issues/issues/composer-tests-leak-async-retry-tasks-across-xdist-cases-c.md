---
title: Composer tests leak async retry tasks between cases, corrupting asyncio.sleep assertions
labels: [area/tests, area/composer, type/bug, needs-triage]
---

Retry tasks started by one Composer service test outlive the case that created them and are
still awaiting a patched `asyncio.sleep` when a later test asserts on that patch. Retry
assertions can therefore pass or fail according to test order rather than behaviour.

## What happens

Tests in `tests/unit/web/composer/test_service.py` patch
`elspeth.web.composer.service.asyncio.sleep` and assert on it directly:
`test_litellm_api_error_is_retried_before_unavailable` asserts `mock_sleep.assert_awaited_once()`,
and `test_bad_request_llm_error_is_not_retried` asserts `mock_sleep.assert_not_awaited()` as
the negative half of the no-retry contract — the backoff sleep is that contract's only
observable side effect besides the provider call count. Sleep activity arriving from an
earlier case makes either assertion report something other than the behaviour under test, and
because the distribution of cases across parallel workers varies between runs, the result is
order-sensitive.

## Why

Not established. The report records the observation and the work required — diagnose the
leaking predecessor, make retry-task and event-loop ownership explicit, and cancel and await
outstanding tasks at teardown — but does not name the test that leaks the task, nor the code
path that leaves it running.

## Fix

- Identify the predecessor whose retry task survives its own case.
- Make retry-task and event-loop ownership explicit in the Composer service test fixtures,
  and cancel and await outstanding tasks at teardown.
- Add an order-sensitive regression proving there is no cross-test sleep activity, so the
  repair is pinned by a test that goes red if the leak returns.

## Note

The original report located the observation at `tests/unit/web/composer/test_service.py:4356`.
At the current tip that line sits inside an unrelated advisor-failure test, so the case it
referred to cannot be recovered from the line number. The `service.asyncio.sleep` assertions
named above are the surfaces the defect corrupts, not necessarily the case that was observed.
