# S5: concurrent composer load and the tutorial RUN stage (spike, 2026-11-13)

Spike for **WP0, the demo-day runbook** ([remediation-plan.md](../remediation-plan.md#demo-day-runbook-wp0)).
Code baseline: `main` at `c987b2b2`. Raw files are in
`.claude/lanes/composer-desk-audit-20261112/spikes/S5-load-and-tutorial-run/`
(abbreviated `S5/` below).

> **The live parts were NOT RUN.** The shared OpenRouter account ran out of
> credit at about 08:44Z. `/api/v1/credits` reported `total_credits 1240` and
> `total_usage 1240.70` (later 1241.16). Every completion returned
> HTTP 402 "Insufficient credits", including a 5-token control request. The
> orchestrator stopped live runs. This note therefore reports:
>
> - **code-level** findings: where the limits are enforced and what users see;
> - **no-provider measurements**: the real admission, worker, queue and SQLite
>   code with a fake provider;
> - **reachability** of `dta-au.github.io`.
>
> Live latency, provider 429 behaviour under real load, and the tutorial run
> itself are listed under [Not run](#not-run-and-resume-commands), with the
> commands to resume them.

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

**Turn durations used in probe A** (all EXECUTED earlier):

- **19.3 s:** the live tutorial-brief planner turn, `evals/composer-live-audit/runs/sessions-t01.log`: 2 provider calls, 6.7 s + 11.2 s.
- **62 s:** the audit's planner-turn p90 (remediation-plan runbook).
- **Deadline 240 s:** `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` in this environment.

**Spend.** This spike used **US$0.0024**: the boot probe ($0.00013) and one
planner call ($0.00227), taken from `usage.cost` in
`runs/instances/S5-load/capture/llm_calls.jsonl`. The three 402s were not
billed. The account-level delta (`S5/spend.log`) is shared with other spikes
and is not attributable to this one.

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
  `web_scrape.py:1323,1401`). So the demo host likely needs **direct** egress
  to `dta-au.github.io:443`, not only an HTTP proxy (INFERRED; it works in
  this container).

## 4. Recommendation for WP0

**Hands-on readiness verdict.** **Not ready for 80 hands-on users on one
instance with this config.** The default is fine for one presenter plus a
handful of people. Expected behaviour if 80 people press Send at once:

- about 44 succeed, in up to 3.7 minutes;
- about 20 get "The composer request timed out";
- about 16 get "The composer is busy. Retry shortly."

That assumes every turn is as fast as the best tutorial turn (19.3 s). At
the planner p90 (62 s), only 12 of 80 succeed.

**The safe concurrency ceiling at default settings is about 8–10 simultaneous
submits** if a p90 wait of about 60 s or less is acceptable. If waits up to
the deadline are tolerable, the ceiling is about 40 for fast turns or about
12 for p90 turns. The tutorial **RUN** for many users is a separate, harder
limit: one pipeline run at a time per process, inside a 270 s held request.

**What an operator must change for 80 people:**

1. **Do not run 80 simultaneous hands-on sessions on one instance.**
   Recommended: the presenter drives, or small groups (≤ 10 at a time)
   stagger their Send. If hands-on at scale is required:
   - `ELSPETH_WEB__COMPOSER_ASYNC_WORKER_CONCURRENCY=16` (the maximum).
     Measured: 40 users p90 65 s, 64 of 80 complete. Risk: the comment at
     `config.py:400` says durable jobs share the 16-thread sync pool
     (`async_workers.py:37`). Saturating it could starve other DB work.
     This is unmeasured; test it.
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
5. **Network.** The demo host needs direct HTTPS egress to `dta-au.github.io`
   (IP-pinned fetch), as well as provider egress.
6. **The per-user composer limit (20/min) needs no change.**

**Proposed edits** (stated, not made):

- **`remediation-plan.md` § Demo-day runbook → Configuration.** Add a bullet:
  > **Concurrency (hands-on only).** One instance runs 4 composer turns at
  > once; queued turns spend their own 240 s deadline, and the 65th waiting
  > turn is refused. For more than about 10 simultaneous users set
  > `COMPOSER_ASYNC_WORKER_CONCURRENCY=16`,
  > `COMPOSER_ASYNC_MAX_QUEUED_OPERATIONS=128` and
  > `COMPOSER_TIMEOUT_SECONDS=420`, raise `AUTH_RATE_LIMIT_PER_MINUTE` and set
  > `TRUSTED_PROXY_HOSTS`; pipeline runs are serial per process, so do not
  > have attendees press Run together (S5).

  Also extend the **Network** bullet with "direct egress (IP-pinned fetch)".
- **Preflight.**
  - Add step 0: "Provider credit and rate limit checked; a 5-token
    completion on the composer and advisor models succeeds".
  - Add step 5: "Load rehearsal: `S5/load_driver.py` at the expected
    concurrency on the demo host; pass = no `composer_queue_full`, no
    deadline expiry, p90 under 90 s".
- **`work-packages.md` § Fix list.** Add a row `Provider 429 not retried
  (S5 §3.4)`, status DECISION. Retrying 429 with backoff inside the deadline
  is a small change in `provider_errors.py`, but it changes audit retry
  semantics. Also add an optional row: deadline counted from admission, so
  late-starting jobs burn provider calls that cannot finish. Candidates are
  to skip a claim when the remaining budget is below a minimum turn, or to
  start the deadline at claim. Neither is a demo blocker.

## 5. Open questions and what would change this

- **Live latency under concurrency.** Does DeepSeek or OpenRouter slow down
  or return 429 at 16 concurrent planner calls? If provider latency rises
  with concurrency, raising the worker count buys less than probe A shows.
- **Tutorial run duration.** If one run takes about 15 s, Run serialisation
  caps simultaneous successful runs at about 18 per 270 s. If it takes
  about 5 s, the cap is about 54. This is the main unknown for hands-on Run.
- **Turn-time writes** to SQLite at 16 concurrent turns, and the 16-thread
  pool shared with durable jobs.
- **The demo deployment's auth mode** (local vs SSO) and proxy chain.
- The prompt guard in `required` mode adds a per-turn external call. Not
  measured here, and the audit did not measure it either.

## Not run, and resume commands

Both items need a funded provider. Run them from `<repo root>`, with
`source .venv/bin/activate` and the `ELSPETH_WEB__*` environment from the
harness README.

```bash
S=.claude/lanes/composer-desk-audit-20261112/spikes/S5-load-and-tutorial-run
# (1) concurrent load: N = 1, 5, 10, 20, then double-submit and the 25-in-a-minute burst
evals/composer-live-audit/instance.sh 8531 S5-load        # users already exist (users.json)
for n in 1 5 10 20; do .venv/bin/python $S/load_driver.py --port 8531 --instance S5-load --n $n --label n$(printf %02d $n) --out $S/load; done
.venv/bin/python $S/load_driver.py --port 8531 --instance S5-load --mode double --label double --out $S/load
.venv/bin/python $S/load_driver.py --port 8531 --instance S5-load --mode burst --burst 25 --label burst25 --out $S/load
# (2) tutorial through RUN on a fresh instance (driver written, never executed)
evals/composer-live-audit/instance.sh 8532 S5-tutorial
.venv/bin/python $S/tutorial_run_driver.py --port 8532 --instance S5-tutorial --out $S/tutorial
# stop each instance by the PID it prints
```

`S5/mock_provider.py` is a loopback provider that replays the recorded tb1
tutorial-planner responses. Pair it with
`ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL=http://127.0.0.1:8533/v1`.

- It was built after the 402 and stopped unused when live runs were halted.
  It answered a single smoke request.
- It could measure full-stack (HTTP + planner + SQLite) capacity without
  provider spend.
- It cannot measure provider behaviour.

No worktree or prototype patch was created; no product code was changed.
