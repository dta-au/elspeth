# S5: concurrent composer load and the tutorial RUN stage (spike, 2026-11-13)

Spike for **WP0, the demo-day runbook** ([remediation-plan.md](../remediation-plan.md#demo-day-runbook-wp0)).
Code baseline: `main` at `c987b2b2` for the morning work, and `c1eb6b29`
for stage 1 (13:23–13:31Z). Raw files are in
`.claude/lanes/composer-desk-audit-20261112/spikes/S5-load-and-tutorial-run/`
(abbreviated `S5/` below).

> **Status.**
>
> - **Morning (08:40–09:00Z): live work stopped.** The shared OpenRouter
>   account ran out of credit (HTTP 402), so the morning produced only:
>   - code-level findings;
>   - no-provider measurements of the admission, worker, queue and SQLite
>     code (§3.1–3.5).
> - **Stage 1 (13:23Z, after the credit was restored): RUN.**
>   - The tutorial ran end to end over the API, 2 runs (§3.6).
>   - The double-submit and 25-in-a-minute burst tests ran (§3.7).
>   - **The tutorial RUN step failed in this container.** The scrape could not
>     reach the pages through this sandbox's egress relay, so the LLM step and
>     the result rows were never reached.
> - **Stage 2 (13:40–14:05Z): RUN** (§3.8).
>   - Live concurrency at N = 1, 5, 10 and 20 simultaneous users, at the
>     default worker concurrency of 4.
>   - Plus N = 20 at concurrency 16, twice, and N = 20 at concurrency 4
>     repeated, as a paired comparison.
>   - 96 live turns. All completed, with no 402, 429 or 503 errors.
> - **Code baseline for stage 2:** `cf97234b`.

## 1. Question and why it matters

The demo has about 80 senior executives, and it is not decided whether they
will use the system hands-on. WP0 needs two answers:

1. What happens when many users compose on one web instance at once?
   Specifically: throughput, latency, the per-user rate limit, SQLite
   contention, provider 429s, and what each failing user sees.
2. Does the tutorial **RUN** stage (a live scrape of the three
   `project-*.html` pages plus an LLM summary) work end to end, and how long
   does it take? The audit never exercised it.

## 2. Method

| Part | What ran | n | Provider |
|---|---|---|---|
| L0 live baseline | One instance (`instance.sh 8531 S5-load`, broad allowlist), 24 extra users added with `elspeth composer users add` and granted `user` the way `instance.sh` does (`S5/setup_users.py`). `S5/load_driver.py` is the N-user concurrent driver; only N=1 ran. | 1 | DeepSeek v4.1 Flash; **402 after the first planner call** |
| A queue probe | `S5/test_s5_queue_probe.py`. Uses the real `ComposerAsyncOperationAuthority.admit`, the real `ComposerAsyncWorker` production loop (`worker.start()`), a file SQLite DB with production PRAGMAs, and the default queue cap of 64. The fake composer sleeps one turn duration. The clock is scaled 1:4, and results are shown in real seconds. | 15 cases, 1 run each | none |
| B quota and contention probe | `S5/test_s5_sqlite_probe.py`. **B1:** the real `RepositoryRateLimitAuthority`, one user, 25 admissions against a limit of 20, with a second user as the negative control. **B2:** N threads released by a barrier perform the full durable admission (session lock, queue count, job insert and quota event in one `BEGIN IMMEDIATE`). | B1 ×1; B2 N=20/80/160 ×1 | none |
| C reachability | `curl` of the three tutorial pages, 3 rounds through the proxy, 1 direct (`--noproxy '*'`), plus a 404 control | 10 requests | none |
| D classifier | `classify_provider_failure` run on constructed litellm `RateLimitError(429)`, `APIError(402)` and `ServiceUnavailableError(503)` | 1 each | none |
| T1 tutorial, stage 1 | `S5/tutorial_run_driver.py` against a fresh instance on 8531. It mirrors the frontend: create the session, rename it to the pending title, GET `/sample`, send the fixed brief, accept the proposal, resolve every pending card, GET `/readiness`, POST `/api/tutorial/run`, then GET `/audit-story`. Run 1 used the main checkout (`S5-tutorial`). Run 2 used worktree `.claude/worktrees/S5-load-and-tutorial-run` with a measurement-only TLS shim (`S5/prototype.patch`); the shim did not change the outcome (§3.6). | 2 | DeepSeek v4.1 Flash composer; **the run's LLM step was never reached** |
| T2 double-submit and burst, stage 1 | `load_driver.py --mode double` and `--mode burst --burst 25` (one user; a fresh session per burst message, spaced 2.2 s apart; each admitted operation is cancelled at once) | 1 each | DeepSeek v4.1 Flash |
| L1 live concurrency, stage 2 | `S5/load_driver.py` via `S5/run_n.sh`: N users released together, one canary first turn each, on fresh sessions. N = 1, 5, 10, 20 at the default concurrency of 4; then N=20 at c=16 (×2) and N=20 at c=4 again, interleaved. | 7 runs, 96 turns | DeepSeek v4.1 Flash |

**Turn durations used in probe A** (all EXECUTED earlier):

- **19.3 s:** the live tutorial-brief planner turn, `evals/composer-live-audit/runs/sessions-t01.log`: 2 provider calls, 6.7 s + 11.2 s.
- **62 s:** the audit's planner-turn p90 (remediation-plan runbook).
- **Deadline 240 s:** `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` in this environment.

**Spend.** Total **US$0.81**, summed from `usage.cost` in each instance's
`capture/llm_calls.jsonl`:

| Phase | Calls | Cost |
|---|---|---|
| Morning: boot probe + one planner call | 2 (the three 402s were not billed) | $0.0024 |
| Stage 1, `S5-tutorial`: tutorial run 1 + double-submit | 5 | $0.0317 |
| Stage 1, `S5-tutorial-run`: tutorial run 2 | 2 | $0.0126 |
| Stage 2, `S5-load`: 7 concurrency runs (§3.8) | 306 | $0.762 (account delta $0.767) |

The burst made no provider calls, because every operation was cancelled
before it started. The morning and stage 1 account deltas in `S5/spend.log`
are shared with other spikes. Stage 2 ran alone on the account, and its
delta matches the captured cost.

**Instrument caveats.**

- SQLite `CURRENT_TIMESTAMP` has 1 s resolution, so probe A times
  transitions in-process by polling every 0.4 real seconds.
- Each deadline is still quantised by up to 4 real seconds.
- Admissions in probe A are sequential, at 20–75 ms each, so 80 arrivals
  spread over about 6 real seconds.
- Controls:
  - B1 has a negative control (a second user is admitted while the first is
    exhausted) and a known positive (the 21st admission is refused).
  - The auth limiter fired on the 21st login (`S5/setup_users.S5-load.log`).
  - Probe A's refusal path fired (known positive: N=80 gave 16 refusals at
    cap 64).
  - The 404 control in C returned 404.

## 3. Results

### 3.1 Limits that apply to many simultaneous users (READ, file:line)

| Limit | Value here | Scope | Where enforced | What the user sees when it trips |
|---|---|---|---|---|
| Composer workers (concurrent turns) | **4** (`composer_async_worker_concurrency`, max 16) | **per process** | `config.py:402`; `app.py:2328` | Nothing at first: the turn sits `queued` behind a normal composing indicator. The wait counts against its deadline (below). |
| Non-terminal composer operations | **64** (`composer_async_max_queued_operations`) | **global**: counts every queued or running job in the DB, all users | `composer_operation_authority.py:366-372`; `operations.py:160,177-185` | HTTP 429 `composer_queue_full`: "The composer is busy. Retry shortly." The frontend shows the refusal and leaves the retry to the user (`frontend/src/api/client.ts:1014-1019`). |
| Turn deadline | **240 s from admission**, not from start | per operation | `deadline_at=now+timedelta(...)` at admission, `composer_operation_authority.py:401`; budget = deadline − now at claim, `composer_async_worker.py:760` | "The composer request timed out. Reload to review the current session state before resubmitting." (504 `composer_operation_deadline_expired`, `composer_operation_authority.py:292-305`), plus the outcome notice "Composition timed out" (`ComposerOperationOutcomeNotice.tsx:5`). |
| Per-user composer budget | **20 per rolling 60 s** | per user, SQL-backed | `app.py:2342`; `rate_limit_authority.py:178-196`; checked inside the admission transaction (`composer_operation_authority.py:425`) | 429 `rate_limited`: "Rate limit exceeded. Try again in N seconds." (`operations.py:164-172`). The same bucket is also charged by `POST /api/tutorial/run` (`tutorial_run_routes.py:63`), shareable reviews and execution. |
| One active operation per session | 1 | per session | unique-insert conflict → `ComposerOperationActiveError` (`composer_operation_authority.py:407-418`), 409 `composer_operation_active` (`operations.py:191`) | The frontend adopts the running operation (`client.ts` `composerActiveOperation`). A double-submit from the UI is benign. |
| Auth (login/register, and failed-auth audit writes) | **20 per minute per client IP** | per `request.client.host`. On SQLite deployments it is in-process (`app.py:2367`). | `rate_limit.py:215-216`; `auth/routes.py:306` | 429 on login: "Rate limit exceeded. Try again in 54 seconds." (observed). With `trusted_proxy_hosts=()` (the default, `config.py` WebSettings), every user behind one proxy, ALB or venue NAT **shares one bucket**. |
| Pipeline execution (Run, including the tutorial) | **1 run at a time per process** | per process | `ThreadPoolExecutor(max_workers=1)`, `execution/service.py:1199`; no admission cap, so runs queue | The tutorial run is a **held HTTP request** that waits at most `transport_idle_ceiling − headroom` = 300 − 30 = **270 s** (`tutorial_service.py:401-406`). After that: 504 "The tutorial run did not finish before the request timeout." (`tutorial_service.py:476`). The run keeps going in the background (INFERRED from the transfer in `_run_live_tutorial`). |
| Provider retries | 3 attempts, 1 s/2 s backoff, retryable failures only | per call | `service.py:3376-3385`; `planning_application.py:1145` | See 3.4. |

The `ComposerChargeableAdmission` in `composer/chargeable_admission.py` and the
custody in `provider_quota.py` are per-operation **authority** checks
(COMPOSE context, quota span). Neither caps concurrency. Token and storage
quotas are off unless a `quota_*` setting is set (`config.py:1349-1366`).
READ.

### 3.2 Simultaneous first turns vs worker concurrency (probe A, EXECUTED, no provider)

Raw rows are in `S5/probe/queue_n*_t*_c*.json`; the log is `S5/probe_queue.log`.

**Default config (concurrency 4, queue cap 64, deadline 240 s), tutorial-brief turn 19.3 s:**

| N simultaneous | completed | timed out (never started / started) | refused 429 queue full | completed p50 | p90 | max |
|---|---|---|---|---|---|---|
| 1 | 1 | 0 | 0 | 20.1 | 20.1 | 20.1 |
| 5 | 5 | 0 | 0 | 21.1 | 39.8 | 39.8 |
| 10 | 10 | 0 | 0 | 40.0 | 59.8 | 61.2 |
| 20 | 20 | 0 | 0 | 61.2 | 101.2 | 101.6 |
| 40 | 40 | 0 | 0 | 101.6 | 181.4 | 201.5 |
| 80 | 44 | 20 (12 / 8) | 16 | 120.7 | 202.0 | 222.8 |

**Default config, turn at the audit's planner p90 (62 s):**

| N | completed | timed out (never / started) | refused | p50 | p90 | max |
|---|---|---|---|---|---|---|
| 1 | 1 | 0 | 0 | 62.8 | 62.8 | 62.8 |
| 5 | 5 | 0 | 0 | 63.7 | 125.3 | 125.3 |
| 10 | 10 | 0 | 0 | 124.9 | 187.5 | 188.9 |
| 20 | 12 | 8 (4 / 4) | 0 | 126.7 | 190.0 | 190.2 |
| 40 | 12 | 28 (24 / 4) | 0 | 126.1 | 189.9 | 190.1 |
| 80 | 12 | 52 (44 / 8) | 16 | 127.2 | 189.9 | 190.5 |

**Concurrency raised to 16 (the setting's maximum), 19.3 s turn:**

| N | completed | refused | p50 | p90 | max | peak concurrent turns |
|---|---|---|---|---|---|---|
| 20 | 20 | 0 | 23.4 | 43.2 | 43.8 | 16 |
| 40 | 40 | 0 | 44.0 | 64.5 | 67.0 | 16 |
| 80 | 64 | 16 | 48.8 | 87.4 | 90.7 | 16 |

Reading:

- Throughput is exactly `concurrency × ⌊240 / turn⌋` per deadline window:
  48 for 19.3 s turns and 12 for 62 s turns at the default.
- Latency grows linearly with queue position, because the queue wait spends
  the turn's own deadline.
- Jobs that started late, 220–238 s after admission, were cut off at
  240 s (`queue_n80_t19_c4.json`). In live use those are provider calls paid
  for and then discarded (INFERRED).
- Peak concurrent turns never exceeded the worker setting. With the default,
  **the bottleneck is the 4-slot worker pool, not CPU or SQLite.**

### 3.3 Per-user quota and SQLite admission contention (probe B, EXECUTED, no provider)

- **B1** (`S5/probe/b1_quota.json`): 20 admitted, 5 refused, first refusal
  at the 21st, `retry_after=60`. The second user's control was admitted.
  - A human cannot reach 20 turns per minute from one session, because a
    session runs one turn at a time (19 s or more each). The per-user limit
    only binds for someone using about 20 sessions or tabs, or for tutorial
    run plus share calls on the same bucket.
  - **Not a factor for the demo.**
- **B2** (`S5/probe/b2_contention_n*.json`): full durable admission, all
  threads released at once, file SQLite (WAL, `busy_timeout=5000`):

| N | outcomes | admission latency p50 / p90 / max (ms) | lock errors |
|---|---|---|---|
| 20 | 20 admitted | 104 / 179 / 240 | 0 |
| 80 | 64 admitted, 16 × 429 queue full | 274 / 429 / 681 | 0 |
| 160 | 64 admitted, 96 × 429 queue full | 535 / 631 / 820 | 0 |

SQLite serialises admissions without "database is locked" errors up to 160
simultaneous submits, and worst-case admission stays under 1 s. Not measured:
SQLite write contention *during* turns (message, state and audit writes from
16 concurrent turns) and from the polling load, which is one GET per user per
second.

### 3.4 Provider failures: what the user sees (EXECUTED: D and L0)

From `S5/classify_429.out`:

- litellm `RateLimitError` (HTTP 429) is not a subclass of litellm
  `APIError`. It falls through to `retryable=False`
  (`provider_errors.py:153`), so **a provider 429 is not retried** and fails
  the turn at once.
- `APIError(402)` and `ServiceUnavailableError(503)` are retryable.
- The user sees "The composer model is unavailable. Retry the request." (the
  `provider_unavailable` reason at `routes/_helpers.py:3067`).

**Credit exhaustion (L0, after the credit ran out; not a capacity result).**
The one live turn behaved as follows:

- The planner call succeeded (12.8 s), then 3 attempts got 402.
- After 18.3 s the user saw "The composer model is unavailable. Retry the
  request." (`error_type composer_planner_failure`).
- `/api/system/status` still reported `composer_available: true`.
- Raw files: `S5/load/n01a.*.json`.

So the demo host shows **no warning** before an exhausted account fails
every turn. This happened to the shared account today.

### 3.5 Tutorial site reachability (EXECUTED, `S5/reachability.txt`)

- All three pages returned 200 (about 2.4 KB each), 0.06–0.39 s through the
  proxy and 0.15 s direct.
- `project-999.html` returned 404 (control).
- The scraper fetches from an IP-pinned address (SSRF defence,
  `web_scrape.py:1323,1401`, `clients/http.py:1913-1917`). Its client sets
  `trust_env=False` unless the audit policy is GENERIC. So it neither uses
  `HTTPS_PROXY` nor reads `SSL_CERT_FILE`.
- **`curl` reachability is not evidence that the scraper will work.** §3.6
  shows exactly that gap in this container.

### 3.6 Tutorial end to end via the API (stage 1, EXECUTED, n=2)

Raw files: `S5/tutorial/` (run 1) and `S5/tutorial-run1/` (run 2). Each holds
`driver.log`, `http_log.json`, `timings.json` and the run record. Run 2
also has `diagnostics.json`, `audit_story_failed_run.json`,
`state_yaml.json` and `egress_relay_failures.jsonl`.

| Step | Run 1 (main checkout) | Run 2 (worktree + TLS shim) |
|---|---|---|
| Create session, rename, `/sample` | 201 / 200 / 200, under 0.05 s each | same |
| Send brief → planner turn completed | 202; **29.6 s**; 2 provider calls (7.1 s discovery, 20.2 s emission); reply "I prepared the requested pipeline and staged it for your review. It has interpretation review cards to resolve before it can run." | 202; **30.4 s**; 2 calls |
| Accept proposal | 200, 0.40 s | 200, 0.40 s |
| Review cards | 4 resolved with 200 (`llm_prompt_template`, `invented_source`, 2 × `pipeline_decision`); 0.9 s in total | same 4; 0.9 s |
| `GET /api/tutorial/{id}/readiness` | 200 (state admitted) | 200 |
| `POST /api/tutorial/run` | **409 after 36.4 s** | **409 after 36.0 s** |
| Total wall time | 67.3 s | 67.6 s |

**The 409 the user gets** (identical in both runs):

```
error_type: tutorial_live_run_failed
status: completed_with_failures
rows_processed: 3, rows_succeeded: 0, rows_failed: 3
"The run produced no output rows. Open the run details to review row errors and source results."
```

**Why it failed** (from `diagnostics.json` and the Landscape `calls` table):

- Each of the 3 rows failed in `web_scrape`: 3 attempts per row, about 2.2 s
  each, with `NetworkError` "Connection error fetching the row's URL".
- The audited call records `ConnectError: [Errno 104] Connection reset by peer`.
- The rows were discarded (`on_error: discard`), so the LLM node never ran.
  With 0 rows reaching the LLM, no summaries and no result rows exist.
- `/audit-story` on the failed run still answers 200 (`llm_call_count: 1`,
  `plugin_versions` for csv, web_scrape, llm, field_mapper and json).
- **The cause is environmental.** This session's egress relay logged 18
  `ws_closed_mid_exchange` failures, "tunnel closed (code 1006) after 2s;
  1513 B sent, 39 B received", for host `185.199.11x.153:443` at exactly the
  scrape timestamps (`egress_relay_failures.jsonl`).
  - It is not the provider and not the product's composer path.
  - I could **not** isolate it: a hand-built IP-pinned `httpx` GET to the same
    IP, with SNI `dta-au.github.io`, the scraper's `X-Abuse-Contact` and
    `X-Scraping-Reason` headers and the relay CA, returns 200 in 0.1–0.2 s.
  - The TLS shim (trusting the relay CA) did not change the outcome.
- **Not measured:**
  - the run's LLM step;
  - its duration;
  - the result rows;
  - a successful audit story.

**What this means for the demo.** The tutorial's RUN step depends on the
demo host's egress path doing exactly what this one failed to do:

- an IP-pinned TLS connection to GitHub Pages;
- no proxy;
- the default (certifi) trust store.

A TLS-inspecting corporate proxy, or an egress relay like this one, gives
exactly this failure:

- about 36 s of spinner (3 rows × 3 attempts × about 2.3 s, plus backoff);
- then "The run produced no output rows".

Runbook preflight step 3 (walk the tutorial through **run**, including the
scrape, on the demo host) is therefore **mandatory**, not optional.

### 3.7 Double-submit and per-user burst (stage 1, EXECUTED, n=1 each)

Raw files: `S5/load/double.*`, `S5/load/burst25.*`.

**Double-submit**, one user and one session:

| Case | Result |
|---|---|
| First message (canary prompt) | 202; completed in 21.3 s; 3 provider calls; proposal staged |
| Same `operation_id` resent 0.3 s later | 202: idempotent, returns the same operation, no new job or quota |
| A second, different message while the first is running | **409** "This session already has a composer request in progress." |

There were no lock errors and no Tracebacks in `web.log`. In the UI the
frontend adopts the running operation (§3.1).

**Burst**, one user, 25 first turns over 55 s, each to its own fresh session:

| Messages | Result |
|---|---|
| #1–#20 | 202; each cancelled at once (`request_cancelled`); 0 provider calls |
| #21–#25 | **429**, starting at 44 s: "Rate limit exceeded. Try again in 16 seconds." then 14, 12, 9 and 7 seconds |

This matches probe B1 live: the limit is 20 per rolling 60 s, and
`Retry-After` counts down to the oldest event's expiry. 5 × 429 appear in
`web.log`. There were no provider 402 or 429 errors anywhere in stage 1
(`provider_failures: 0` in every summary).

### 3.8 Live concurrency (stage 2, EXECUTED)

**Setup.**

- **Instance:** one `instance.sh 8531 S5-load` (broad allowlist, SQLite,
  local auth).
- **Users:** N distinct users (`s5u01`…), each with a fresh session. All N
  are released by one barrier and send the same first turn: the battery
  `canary` prompt, a simple made-up build ("three colours … read in and
  written straight back out to JSON").
- **Models:** composer DeepSeek v4.1 Flash. No advisor call was made on
  these planner turns.
- **Driver:** `S5/load_driver.py`, polling at 1 s like the frontend. Its
  sampler records:
  - server CPU from `/proc`, every 0.5 s;
  - latency of `GET /api/health`, a bare async handler (`app.py:2757`), as
    the event-loop-lag proxy.
- **Cost:** from `usage.cost` in each call captured during the run window.
  Credit was checked before each N (`S5/spend.log`).
- **Each configuration ran once (n=1),** except N=20, which ran twice at c=4
  and twice at c=16. Interleave order: c4a, c16a, c16b, c4b.

**Definitions.**

- **TTFB:** latency of the `POST /messages` 202 admission. Composer replies
  are not streamed, so the user's first sign of progress is the admission
  plus the progress indicator.
- **Queue wait:** time until the first poll that sees `running`, at 1 s
  resolution.
- **Completion:** submit to terminal.

| Config | n | TTFB p50 / p95 / max (s) | Queue wait p50 / max (s) | Completion p50 / p95 / max (s) | Rejected (429/503) | Timed out | Provider calls / failures | Provider call median / max (s) | `/api/health` p50 / p95 / max (ms) | Server CPU mean / max (%) | Cost (US$) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| N=1, c=4 | 1 | 0.03 / 0.03 / 0.03 | 1.0 / 1.0 | 15.2 / 15.2 / 15.2 | 0 | 0 | 3 / 0 | 3.3 / 9.6 | 2.3 / 3.1 / 3.7 | 10 / 74 | 0.028 |
| N=5, c=4 | 5 | 0.10 / 0.15 / 0.15 | 1.3 / 30.9 | 35.9 / 62.1 / 62.1 | 0 | 0 | 16 / 0 | 10.8 / 28.1 | 2.3 / 9.6 / 47 | 13 / 99 | 0.117 |
| N=10, c=4 | 10 | 0.16 / 0.46 / 0.46 | 25.2 / 65.5 | 55.2 / 116.5 / 116.5 | 0 | 0 | 32 / 0 | 9.2 / 51.8 | 2.4 / 14.9 / 242 | 16 / 95 | 0.109 |
| N=20, c=4 (a) | 20 | 0.31 / 0.66 / 0.70 | 33.0 / 72.7 | 50.8 / 88.8 / 99.7 | 0 | 0 | 63 / 0 | 4.6 / 21.5 | 2.5 / 45 / 104 | 32 / 127 | 0.117 |
| N=20, c=4 (b) | 20 | 0.32 / 0.72 / 0.74 | 38.5 / 73.8 | 49.5 / 75.9 / 91.0 | 0 | 0 | 64 / 0 | 3.6 / 16.2 | 2.3 / 77 / 166 | 33 / 123 | 0.118 |
| N=20, c=16 (a) | 20 | 0.38 / 1.27 / 4.38 | 1.9 / 26.0 | 28.3 / 40.2 / 62.0 | 0 | 0 | 61 / 0 | 5.3 / 27.4 | 2.6 / 88 / 128 | 52 / 127 | 0.150 |
| N=20, c=16 (b) | 20 | 0.33 / 1.16 / 1.17 | 1.4 / 30.6 | 35.6 / 64.7 / 69.1 | 0 | 0 | 67 / **4** | 8.3 / 26.3 | 2.6 / 160 / 642 | 48 / 132 | 0.124 |

**Raw per-user rows.** Each entry is `user: TTFB / queue wait / completion
(s) outcome`, generated from the `*.requests.json` files:

- **N=1, c=4** (`load/s2_n01.requests.json`): 01: 0.03 / 1.0 / 15.2 OK
- **N=5, c=4** (`load/s2_n05.requests.json`): 01: 0.13 / 1.3 / 43.7 OK; 02: 0.10 / 1.3 / 31.5 OK; 03: 0.05 / 1.3 / 30.8 OK; 04: 0.15 / 30.9 / 62.1 OK; 05: 0.07 / 1.3 / 35.9 OK
- **N=10, c=4** (`load/s2_n10.requests.json`): 01: 0.20 / 46.0 / 68.5 OK; 02: 0.18 / 38.4 / 55.2 OK; 03: 0.14 / 1.4 / 38.2 OK; 04: 0.14 / 30.2 / 91.2 OK; 05: 0.11 / 1.3 / 25.6 OK; 06: 0.46 / 65.5 / 103.4 OK; 07: 0.41 / 25.2 / 46.0 OK; 08: 0.43 / 55.2 / 116.5 OK; 09: 0.08 / 0.2 / 65.5 OK; 10: 0.16 / 0.4 / 30.1 OK
- **N=20, c=4 (run a)** (`load/s2_n20.requests.json`): 01: 0.18 / 50.8 / 68.5 OK; 02: 0.57 / 26.5 / 39.1 OK; 03: 0.20 / 1.6 / 11.8 OK; 04: 0.26 / 50.0 / 67.5 OK; 05: 0.36 / 33.0 / 49.8 OK; 06: 0.34 / 28.7 / 38.1 OK; 07: 0.31 / 53.9 / 72.7 OK; 08: 0.21 / 1.5 / 14.2 OK; 09: 0.22 / 39.1 / 57.1 OK; 10: 0.37 / 14.2 / 50.8 OK; 11: 0.32 / 67.1 / 88.8 OK; 12: 0.60 / 14.2 / 32.8 OK; 13: 0.15 / 1.5 / 13.3 OK; 14: 0.65 / 72.7 / 87.8 OK; 15: 0.28 / 57.6 / 71.1 OK; 16: 0.66 / 68.5 / 81.6 OK; 17: 0.70 / 72.0 / 99.7 OK; 18: 0.29 / 12.0 / 28.6 OK; 19: 0.49 / 39.1 / 53.9 OK; 20: 0.18 / 1.7 / 26.1 OK
- **N=20, c=4 (run b)** (`load/s2_n20_c4b.requests.json`): 01: 0.16 / 0.4 / 25.0 OK; 02: 0.30 / 22.1 / 40.0 OK; 03: 0.35 / 49.8 / 62.4 OK; 04: 0.23 / 48.5 / 64.3 OK; 05: 0.62 / 52.5 / 65.2 OK; 06: 0.56 / 28.3 / 38.8 OK; 07: 0.72 / 65.6 / 74.9 OK; 08: 0.65 / 31.9 / 44.7 OK; 09: 0.43 / 39.5 / 49.5 OK; 10: 0.15 / 1.2 / 21.6 OK; 11: 0.74 / 24.6 / 31.9 OK; 12: 0.21 / 63.4 / 75.9 OK; 13: 0.57 / 53.8 / 75.9 OK; 14: 0.59 / 45.3 / 53.5 OK; 15: 0.26 / 38.5 / 52.5 OK; 16: 0.32 / 73.8 / 91.0 OK; 17: 0.14 / 1.2 / 26.0 OK; 18: 0.62 / 64.3 / 73.2 OK; 19: 0.14 / 1.2 / 27.4 OK; 20: 0.19 / 27.3 / 47.5 OK
- **N=20, c=16 (run a)** (`load/s2_n20_c16a.requests.json`): 01: 0.24 / 1.4 / 24.7 OK; 02: 0.18 / 1.8 / 35.4 OK; 03: 0.14 / 1.3 / 40.2 OK; 04: 1.10 / 15.9 / 35.6 OK; 05: 1.01 / 19.9 / 35.2 OK; 06: 0.38 / 2.7 / 25.9 OK; 07: 0.83 / 1.1 / 36.2 OK; 08: 0.27 / 1.4 / 28.3 OK; 09: 0.79 / 2.2 / 30.5 OK; 10: 0.67 / 2.9 / 25.9 OK; 11: 0.18 / 1.4 / 24.5 OK; 12: 1.27 / 22.2 / 62.0 OK; 13: 0.21 / 2.4 / 15.9 OK; 14: 0.30 / 1.4 / 21.0 OK; 15: 0.31 / 2.6 / 23.6 OK; 16: 4.38 / 26.0 / 37.6 OK; 17: 0.59 / 1.9 / 38.6 OK; 18: 0.42 / 1.8 / 37.8 OK; 19: 0.34 / 1.5 / 26.8 OK; 20: 0.50 / 4.9 / 20.7 OK
- **N=20, c=16 (run b)** (`load/s2_n20_c16b.requests.json`): 01: 0.27 / 1.4 / 37.9 OK; 02: 0.16 / 1.3 / 34.9 OK; 03: 0.77 / 1.8 / 29.0 OK; 04: 0.20 / 1.3 / 35.1 OK; 05: 1.15 / 30.6 / 64.7 OK; 06: 0.18 / 1.4 / 33.3 OK; 07: 0.55 / 1.7 / 22.7 OK; 08: 0.33 / 1.4 / 29.9 OK; 09: 0.14 / 1.3 / 46.2 OK; 10: 0.35 / 1.4 / 35.6 OK; 11: 0.46 / 1.5 / 49.3 OK; 12: 0.23 / 0.9 / 64.7 OK; 13: 0.16 / 2.3 / 26.9 OK; 14: 0.21 / 1.1 / 24.7 OK; 15: 1.16 / 26.7 / 45.6 OK; 16: 1.01 / 24.6 / 38.4 OK; 17: 0.97 / 1.0 / 33.8 OK; 18: 0.28 / 1.3 / 35.6 OK; 19: 1.17 / 29.8 / 69.1 OK; 20: 0.40 / 1.4 / 36.0 OK

**Reading.**

- **Every one of the 96 turns completed.**
  - No 429 `rate_limited`, no 429 `composer_queue_full`, no 503 and no
    deadline expiry.
  - No `database is locked`, `OperationalError` or Traceback in `web.log`
    (`web_log` in each summary).
  - Every user saw the normal staged-proposal reply.
- **Provider errors:** none were 402 or 429.
  - c16b had 4 transient transport failures ("Server disconnected" and 3 ×
    "Response payload is not completed").
  - These are litellm `APIError`, so they were retried, and all 20 users
    still completed.
  - This possibly comes from this sandbox's relay at 16 concurrent tunnels;
    it is not attributed.
- **At c=4 the bottleneck is the worker pool,** exactly as probe A predicted.
  - Users 5+ wait in the queue: up to 31 s at N=5, 66 s at N=10 and 74 s
    at N=20.
  - N=20 completion p95 was 76–89 s and max 91–100 s.
  - N=20 finished faster than N=10 because the provider was faster during
    those runs: call median 3.6–4.6 s vs 9.2 s, max 16–22 s vs 52 s.
  - **Provider latency variance between runs is as large as the
    concurrency effect.** Treat single runs as indicative.
- **At c=16, N=20 completion p95 was 40–65 s (max 62–69 s), against 76–89 s
  at c=4.**
  - The queue wait for the users beyond 16 was still up to 26–31 s.
  - The cost is a busier single process:
    - CPU mean about 50% vs 33%, with peaks above one core (127–132%);
    - `/api/health` p95 88–160 ms (max 642 ms) vs 45–77 ms;
    - TTFB max 1.2–4.4 s vs about 0.7 s.
  - So the event loop is not saturated at 20, but its headroom shrinks.
    Extrapolating to 40+ concurrent turns on one process is not supported
    by these data.
- **Cost:** about US$0.006–0.03 per canary turn. Stage 2 total: **US$0.762**
  from captures, against an account delta of US$0.767 (13.0926 → 12.3253
  remaining), so the two instruments agree.

## 4. Recommendation for WP0

**Hands-on readiness verdict.** **Not ready for 80 hands-on users on one
instance with this config.** The default is fine for one presenter plus a
handful of people. Expected behaviour if 80 people press Send at once:

- about 44 succeed, in up to 3.7 minutes;
- about 20 get "The composer request timed out";
- about 16 get "The composer is busy. Retry shortly."

That assumes every turn is as fast as the best tutorial turn (19.3 s). At
the planner p90 (62 s), only 12 of 80 succeed. These 80-user figures come
from probe A (fake provider). The live N ≤ 20 runs in §3.8 behave as that
model predicts: the queue forms at the worker cap, and wait grows with
queue position.

**Safe number of simultaneous live users** (stage 2 measured, probe A
projected). "Simultaneous" means pressing Send within the same few seconds.
People typing at their own pace spread the load further.

| Worker concurrency | Up to this many simultaneous users | Expected wait | Evidence |
|---|---|---|---|
| 4 (default) | **5** for a p95 of about 1 min or less; 20 still all complete | N=5: p95 62 s; N=10: p95 117 s; N=20: p95 76–89 s, max 100 s | live, §3.8 |
| 16 | **20** for a p95 of about 1 min or less, all completing | N=20: p95 40–65 s, max 62–69 s | live, §3.8 (×2) |
| 16 | about 40 (projected) | p90 about 65 s if turns take about 19 s; slower turns scale linearly | probe A only. Not live: event-loop headroom at 40 is unmeasured. |
| any | more than 64 waiting turns are refused | — | queue cap, §3.1 |

The tutorial **RUN** for many users is a separate, harder limit: one
pipeline run at a time per process, inside a 270 s held request.

**The tutorial RUN step has still never been seen to succeed.**

- In stage 1 the build half worked twice: brief → proposal → 4 cards →
  readiness in about 31 s.
- Run then failed in about 36 s because the scrape could not get out of this
  container (§3.6).
- Until the preflight walk passes **on the demo host**, treat Run as
  unverified for both presenter and attendees.

**What an operator must change for 80 people:**

1. **Do not run 80 simultaneous hands-on sessions on one instance.**
   Recommended: the presenter drives, or groups of **20 or fewer** take
   turns pressing Send (5 or fewer if the config stays at the default).
   For any hands-on segment, set:
   - `ELSPETH_WEB__COMPOSER_ASYNC_WORKER_CONCURRENCY=16` (the maximum).
     - **Live-measured:** N=20 p95 dropped from 76–89 s to 40–65 s, with all
       turns completed and no errors (§3.8).
     - **Cost:** server CPU about 50% mean, with peaks above one core, and
       event-loop p95 up to 160 ms.
     - **Risk:** the comment at `config.py:400` says durable jobs share the
       16-thread sync pool (`async_workers.py:37`). Saturating it could
       starve other DB work. That was not observed at N=20.
   - `ELSPETH_WEB__COMPOSER_ASYNC_MAX_QUEUED_OPERATIONS=128` or more, so
     that users 65–80 are not refused with "busy".
   - `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=420` or more, because queue wait
     spends the deadline. Do not exceed what the async polling path tolerates;
     the sync path is bounded separately by
     `composer_transport_idle_ceiling_seconds`.
   - More than one replica needs the PostgreSQL session store. The SQLite
     auth and write limiters are in-process (`app.py:2361-2367`); INFERRED,
     confirm the deployment contract.
2. **Login burst.** With local auth, set
   `ELSPETH_WEB__AUTH_RATE_LIMIT_PER_MINUTE` to 100 or more, **and** set
   `ELSPETH_WEB__TRUSTED_PROXY_HOSTS` to the real proxy. Otherwise all 80
   logins share one 20-per-minute bucket and users 21+ wait up to a minute.
   SSO (OIDC) logins do not hit `/api/auth/login`; only failed-token audit
   writes use this limiter.
3. **Provider headroom.**
   - Pre-fund the provider account and check its per-minute limits for 16
     concurrent planner calls. A provider 429 is not retried and shows "The
     composer model is unavailable".
   - Add a credit and quota check to the preflight. `composer_available`
     does not detect exhaustion.
   - Indicative cost: the tutorial planner call was $0.0023 for about 60k
     prompt tokens.
4. **Tutorial RUN.** Do not let 80 people press Run together:
   - runs execute one at a time per process (`execution/service.py:1199`);
   - the request gives up after 270 s;
   - users beyond about 270 s ÷ (one run's duration) see "The tutorial run
     did not finish before the request timeout".
   
   That duration is unmeasured (see Not run). Either the presenter runs it,
   or attendees stop at "Continue to Run". The 270 s comes from
   `composer_transport_idle_ceiling_seconds`. Raising it requires every
   proxy hop to allow it.
5. **Network.** The demo host needs HTTPS egress to `dta-au.github.io`
   that works for the scraper's IP-pinned, no-proxy, certifi-trusting
   connection, as well as provider egress.
   - A TLS-inspecting proxy breaks it, and so does this sandbox's relay
     (§3.6).
   - The symptom is about 36 s, then "The run produced no output rows".
   - Verify it with the real tutorial Run on the demo host; a `curl`
     check is not enough.
6. **The per-user composer limit (20/min) needs no change.**

**Proposed edits** (stated, not made):

- **`remediation-plan.md` § Demo-day runbook → Configuration.** Add a bullet:
  > **Concurrency.** Set `ELSPETH_WEB__COMPOSER_ASYNC_WORKER_CONCURRENCY=16`
  > (default 4), `ELSPETH_WEB__COMPOSER_ASYNC_MAX_QUEUED_OPERATIONS=128`
  > (default 64) and `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=420` (queued time
  > spends the deadline). With these, one instance served 20 simultaneous
  > first turns with p95 40–65 s and no errors (S5 §3.8). Keep each
  > hands-on wave to **20 or fewer people pressing Send together** (5 or
  > fewer at the default concurrency). With local auth, raise
  > `AUTH_RATE_LIMIT_PER_MINUTE` and set `TRUSTED_PROXY_HOSTS`. Pipeline
  > runs are serial per process, so do not have attendees press Run
  > together.

  Also extend the **Network** bullet with "egress usable by the IP-pinned,
  no-proxy scraper; a curl check is not evidence (S5 §3.6)". In Preflight
  step 3, add "pass = `POST /api/tutorial/run` returns 200 with 3 rows (or
  `tutorial_run_driver.py` exits with `run_status` 200)".
- **Preflight.**
  - Add step 0: "Provider credit and rate limit checked; a 5-token
    completion on the composer and advisor models succeeds".
  - Add step 5: "Load rehearsal on the demo host, with the guard in
    `required` mode: `S5/load_driver.py --n 20`; pass = all `completed`, no
    `composer_queue_full` and no deadline expiry, p95 under 90 s". Stage 2
    shows a provider-latency swing between runs as large as the concurrency
    effect, so run it twice.
- **`work-packages.md` § Fix list.** Add a row `Provider 429 not retried
  (S5 §3.4)`, status DECISION. Retrying 429 with backoff inside the deadline
  is a small change in `provider_errors.py`, but it changes audit retry
  semantics. Also add an optional row: deadline counted from admission, so
  late-starting jobs burn provider calls that cannot finish. Candidates are
  to skip a claim when the remaining budget is below a minimum turn, or to
  start the deadline at claim. Neither is a demo blocker.

## 5. Open questions and what would change this

- **Live latency under concurrency.** At 16 concurrent planner calls the
  provider returned no 429 and per-call latency did not rise systematically
  (§3.8). But run-to-run provider variance (call median 3.6–10.8 s) was as
  large as the concurrency effect. Not measured:
  - more than 20 simultaneous users live;
  - the advisor on edit turns;
  - the prompt guard in `required` mode, which adds an external call per
    turn.
- **Why the scraper's connection is reset here when a hand-built pinned
  request is not** (§3.6). If the cause is in the scraper's TLS or connection
  setup rather than this sandbox's relay, it also affects real hosts. A
  packet capture, or the scraper run under `S5_SPIKE_CA_FILE` with httpx
  debug logging, would settle it.
- **Tutorial run duration.** If one run takes about 15 s, Run serialisation
  caps simultaneous successful runs at about 18 per 270 s. If it takes
  about 5 s, the cap is about 54. This is the main unknown for hands-on Run.
- **Turn-time writes** to SQLite at 16 concurrent turns, and the 16-thread
  pool shared with durable jobs.
- **The demo deployment's auth mode** (local vs SSO) and proxy chain.
- The prompt guard in `required` mode adds a per-turn external call. Not
  measured here, and the audit did not measure it either.

## Not run, and resume commands

| Item | Status |
|---|---|
| Stage 1: tutorial via the API, double-submit, burst | **Done** (§3.6, §3.7). The tutorial RUN failed on this container's egress. |
| Stage 2: live concurrency at N = 1, 5, 10, 20 (+ N=20 at c=16 ×2, c=4 ×2) | **Done** (§3.8). |
| Live N above 20, and 80 users | **NOT RUN.** Projection only (probe A). |
| Tutorial RUN success path: duration, result rows, audit story | **NOT MEASURED.** It needs a host whose egress the scraper can use. |

Run these from `<repo root>`, with `source .venv/bin/activate` and the
`ELSPETH_WEB__*` environment from the harness README:

```bash
S=.claude/lanes/composer-desk-audit-20261112/spikes/S5-load-and-tutorial-run
# Concurrency rerun (24 users exist in runs/instances/S5-load/users.json); add
# ELSPETH_WEB__COMPOSER_ASYNC_WORKER_CONCURRENCY=16 before instance.sh for the c=16 arm
evals/composer-live-audit/instance.sh 8531 S5-load
$S/run_n.sh 20 rerun_n20      # prints credit before/after and the summary
# Tutorial RUN on a host with working egress (fresh instance)
evals/composer-live-audit/instance.sh 8531 S5-tutorial-host
.venv/bin/python $S/tutorial_run_driver.py --port 8531 --instance S5-tutorial-host --out $S/tutorial-host
# stop each instance by the PID it prints
```

`--instance` also accepts an absolute instance directory; run 2 used the
worktree's directory that way.

**Leftovers from this spike:**

- **Worktree.** `.claude/worktrees/S5-load-and-tutorial-run` (branch
  `spike/S5-load-and-tutorial-run`) holds only the measurement shim in
  `S5/prototype.patch`. It makes the SSRF-safe client trust
  `$S5_SPIKE_CA_FILE`. It is not for merge, and it did not change the
  outcome.
- **Mock provider.** `S5/mock_provider.py` is a loopback provider that
  replays the recorded tb1 tutorial-planner responses (pair it with
  `ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL=http://127.0.0.1:8533/v1`).
  - It was built during the 402 outage and never used for a measurement.
  - It could measure full-stack capacity without provider spend.
  - It cannot measure provider behaviour.
- **Product code.** None was changed in the main checkout.
