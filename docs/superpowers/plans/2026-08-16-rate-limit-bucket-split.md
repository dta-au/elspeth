# Rate-Limit Bucket Split + Tutorial-Completion Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop cheap DB-write endpoints from sharing (and exhausting) the per-user rate-limit bucket sized for LLM-backed composer calls, and make the tutorial-completion save survive a transient 429.

**Architecture:** Add a second `ComposerRateLimiter` instance (`app.state.write_rate_limiter`, default 60/min) for cheap authenticated DB writes; the existing `app.state.rate_limiter` (deployed at 10/min) stays dedicated to LLM/execution-backed endpoints. On the frontend, surface the 429 envelope's `retry_after` in `ApiError` and give `markTutorialGraduated` one delayed retry.

**Tech Stack:** FastAPI dependencies + pydantic `WebSettings` (backend); zustand store + typed fetch client (frontend); pytest + vitest.

**Spec:** No standalone spec — the diagnosis is recorded in the incident investigation summarized below and in the commit that ships the first slice (audit-readiness reads unguarded, `test_audit_readiness_reads_do_not_consume_the_composer_rate_limit`).

## Background (read before Task 1)

Diagnosed 2026-08-16 on the live host: the deployed limit is
`ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=10` (`deploy/elspeth-web.env`),
and ONE per-user sliding-window bucket (`app.state.rate_limiter`) was
checked by every rate-limited endpoint. The tutorial's endgame fires ~5
`PATCH /api/composer-preferences` stage-persist writes in <30 s
(`HelloWorldTutorial.tsx` persists on every stage transition) plus guided
turns, `POST /api/tutorial/run`, and audit-readiness polling — so the
tutorial-completion PATCH landed 11th in the window and got
`429 Rate limit exceeded. Try again in 26 seconds`, surfacing as
"Couldn't save tutorial completion."

Slice already shipped separately: audit-readiness GETs no longer touch any
limiter (reads are unguarded — policy stated in
`src/elspeth/web/preferences/routes.py`).

Bucket assignment after this plan:

| Endpoint | Bucket | Why |
|---|---|---|
| POST compose / messages / guided respond / guided plan | strict (`rate_limiter`) | LLM-backed |
| POST /api/tutorial/run | strict | executes a pipeline with provider calls |
| POST execution run (`execution/routes.py`) | strict | executes a pipeline |
| GET /api/sessions/shared/{token} (`get_shared_inspect`, `shareable_reviews/routes.py`) | strict | abuse-sensitive token probe; one-off action, not a UI loop |
| PATCH /api/composer-preferences | **write** (`write_rate_limiter`) | cheap DB upsert; tutorial fires bursts |
| POST mark-ready-for-review + GET shareable link (`shareable_reviews/routes.py`, 1st + 2nd check sites) | **write** | cheap DB write / token mint |
| auth endpoints | unchanged (`auth_rate_limiter`, per-IP) | separate concern |

## Global Constraints

- New settings name is `write_rate_limit_per_minute` → env `ELSPETH_WEB__WRITE_RATE_LIMIT_PER_MINUTE`; default **60**, `ge=1`. It MUST have a default (a required field would brick every existing deployment env file).
- The minimum-image floor does NOT move: `test_documented_minimum_image_revision_is_the_true_settings_floor` (`tests/unit/deployment/test_aws_ecs_terraform_package.py`, `_shipped_web_settings_environment` ~lines 148–161/3564–3659) derives the required-settings set ONLY from git-tracked `.tf` files under `deploy/aws-ecs/terraform/` — and this plan deliberately does NOT wire the new var into terraform (safe default; AWS stack is torn down). Do NOT edit the README "Minimum image revision" paragraph: with `required` unchanged, naming a newer commit would break the test's minimality assertion (`assert not required <= earlier`).
- Read `docs/agents/recent-code-hints.md` before writing code (repo rule). Run the FULL `pytest tests/` (CI-equivalent) before calling the branch done — whole-tree gates (masquerade, attribute-contracts) scan tests too: no new `getattr`/`hasattr` anywhere, including test files.
- Work on `release/0.7.2` in the shared checkout: stage by pathspec only (never `git add -A`), verify `git log -1` after each commit.
- Frontend tests: run vitest from `src/elspeth/web/frontend/` (repo-root vitest walks everything).

---

### Task 1: Backend write bucket — settings, app wiring, dependency

**Files:**
- Modify: `src/elspeth/web/config.py` (beside `composer_rate_limit_per_minute: int = Field(..., ge=1)`, ~line 285)
- Modify: `src/elspeth/web/middleware/rate_limit.py` (beside `get_rate_limiter`, ~line 142)
- Modify: `src/elspeth/web/app.py` (rate-limiter block, ~lines 1399–1410)
- Test: `tests/unit/web/middleware/test_rate_limit.py` — this file ALREADY EXISTS (~246 lines). Append the new tests and MERGE any new imports into the existing top-of-file import block; a duplicate `ComposerRateLimiter` import (or re-declared `from fastapi import ...`) trips ruff F811 in pre-commit.

**Interfaces:**
- Produces: `WebSettings.write_rate_limit_per_minute: int` (default 60); `app.state.write_rate_limiter: ComposerRateLimiter`; async dependency `get_write_rate_limiter(request: Request) -> ComposerRateLimiter` returning `request.app.state.write_rate_limiter`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/web/middleware/test_rate_limit.py (append; imports shown here
# must be MERGED into the file's existing import block, not re-declared)
# needs: FastAPI, Request, ComposerRateLimiter, get_rate_limiter,
#        get_write_rate_limiter


def test_write_rate_limiter_dependency_reads_distinct_app_state() -> None:
    """get_write_rate_limiter must return app.state.write_rate_limiter,
    never the strict composer bucket — the whole point of the split."""
    import asyncio

    app = FastAPI()
    app.state.rate_limiter = ComposerRateLimiter(limit=1)
    app.state.write_rate_limiter = ComposerRateLimiter(limit=60)
    scope = {"type": "http", "app": app, "headers": []}
    request = Request(scope)
    strict = asyncio.run(get_rate_limiter(request))
    write = asyncio.run(get_write_rate_limiter(request))
    assert write is app.state.write_rate_limiter
    assert strict is app.state.rate_limiter
    assert write is not strict


def test_web_settings_write_rate_limit_defaults_to_60() -> None:
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    assert settings.write_rate_limit_per_minute == 60
```

(Mirror the required-fields kwargs from any existing `WebSettings(...)`
construction in `tests/unit/web/` — copy an existing one verbatim if the
set above is stale.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/unit/web/middleware/test_rate_limit.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_write_rate_limiter'`.

- [ ] **Step 3: Implement**

`config.py`, immediately after `composer_rate_limit_per_minute`:

```python
    write_rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        description=(
            "Per-user per-minute budget for cheap authenticated DB-write "
            "endpoints (composer-preferences PATCH, shareable-review "
            "mark-ready/link). Deliberately a SEPARATE bucket from "
            "composer_rate_limit_per_minute, which is sized for LLM-backed "
            "calls: the tutorial legitimately writes preference resume "
            "state in bursts, and sharing one bucket let those bursts "
            "starve the tutorial-completion write into a 429."
        ),
    )
```

`middleware/rate_limit.py`, after `get_rate_limiter`:

```python
async def get_write_rate_limiter(request: Request) -> ComposerRateLimiter:
    """FastAPI dependency for the cheap-DB-write bucket.

    Distinct from get_rate_limiter (the LLM/execution bucket) so bursty
    but cheap writes — tutorial stage persistence above all — cannot
    starve LLM-endpoint budget or vice versa.
    """
    limiter: ComposerRateLimiter = request.app.state.write_rate_limiter
    return limiter
```

`app.py`, after the existing `app.state.rate_limiter = ...` block:

```python
    # --- Write rate limiter (per-process in-memory) ---
    # Cheap authenticated DB writes get their own bucket so tutorial
    # preference bursts never compete with the LLM-call budget above.
    app.state.write_rate_limiter = ComposerRateLimiter(
        limit=settings.write_rate_limit_per_minute,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/web/middleware/test_rate_limit.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/config.py src/elspeth/web/middleware/rate_limit.py src/elspeth/web/app.py tests/unit/web/middleware/test_rate_limit.py
git commit -m "feat(web): add per-user write rate-limit bucket distinct from the composer LLM bucket"
git log -1  # shared checkout: confirm the commit is yours and complete
```

---

### Task 2: Move the preferences PATCH onto the write bucket

**Files:**
- Modify: `src/elspeth/web/preferences/routes.py` (~lines 23, 47, 54)
- Modify: `tests/unit/web/preferences/test_routes_mode_telemetry.py` (~line 63 wires `app.state.rate_limiter`)
- Modify: `tests/integration/web/test_preferences_routes.py` — `_make_app` (~lines 38–90) also wires only `app.state.rate_limiter` and serves ~15 tests. Its current signature takes `rate_limit: int = 100` sizing the strict bucket, and `test_patch_enforces_rate_limit_returns_429` (~line 311) passes `rate_limit=2` to starve it. Required change: keep `rate_limit` sizing the strict bucket, ADD a second parameter `write_rate_limit: int = 100` sizing `app.state.write_rate_limiter = ComposerRateLimiter(limit=write_rate_limit)`, and re-point `test_patch_enforces_rate_limit_returns_429` to pass `write_rate_limit=2` (leaving the strict bucket at its default) with its docstring updated — it now pins the WRITE bucket.
- Test: `tests/unit/web/preferences/test_routes_mode_telemetry.py` (new test) — plus sweep `git grep -ln "rate_limiter" tests/ | xargs grep -l composer-preferences` for any other fixtures wiring the old bucket for this route.

**Interfaces:**
- Consumes: `get_write_rate_limiter` from Task 1.
- Produces: `PATCH /api/composer-preferences` checks ONLY `app.state.write_rate_limiter`.

- [ ] **Step 1: Write the failing test**

In `tests/unit/web/preferences/test_routes_mode_telemetry.py`, copy the
existing app-fixture pattern (the function that sets
`app.state.rate_limiter = ComposerRateLimiter(limit=100)`) and add:

```python
def test_preferences_patch_uses_the_write_bucket_not_the_composer_bucket() -> None:
    """A starved composer bucket must not block preference writes, and
    the write bucket must actually meter them.

    This is the tutorial-graduation incident regression test: with the
    old shared bucket, limit=1 on the composer bucket 429'd the second
    PATCH."""
    app = _build_app()  # reuse this module's existing app factory
    app.state.rate_limiter = ComposerRateLimiter(limit=1)
    app.state.write_rate_limiter = ComposerRateLimiter(limit=3)
    with TestClient(app) as client:
        for _ in range(3):
            response = client.patch("/api/composer-preferences", json={})
            assert response.status_code == 200
        # Write bucket exhausted -> 429 WITH retry_after in the envelope
        # (the frontend retry in Task 5/6 depends on that field).
        response = client.patch("/api/composer-preferences", json={})
    assert response.status_code == 429
    assert response.json()["detail"]["retry_after"] >= 1
```

(Adjust `_build_app` to whatever the module's factory is actually named;
add `app.state.write_rate_limiter = ComposerRateLimiter(limit=100)` to
that shared factory so existing tests keep passing once the route flips.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/web/preferences/ -q`
Expected: the new test FAILS at the second loop iteration (429 from the
starved composer bucket — proving the route still reads the old bucket).

- [ ] **Step 3: Flip the route**

In `preferences/routes.py`: import `get_write_rate_limiter` instead of
`get_rate_limiter`; change the PATCH signature line to
`rate_limiter: ComposerRateLimiter = Depends(get_write_rate_limiter),  # noqa: B008`
and update the "Panel C1" comment to say the write is metered by the
WRITE bucket (cheap DB upsert; tutorial resume-state bursts are
legitimate) while reads stay unguarded.

- [ ] **Step 4: Run to verify green**

Run: `python -m pytest tests/unit/web/preferences/ tests/integration/web/ -q`
Expected: PASS (integration sweep catches any full-app fixture that only
wires the old bucket).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/preferences/routes.py tests/unit/web/preferences/ tests/integration/web/test_preferences_routes.py
git commit -m "fix(web): meter composer-preferences PATCH on the write bucket so tutorial bursts cannot 429 the completion save"
git log -1
```

---

### Task 3: Move shareable-review mark-ready + link-mint to the write bucket

**Files:**
- Modify: `src/elspeth/web/shareable_reviews/routes.py` (check sites at ~lines 72 and 106 move; ~line 140 — inside `get_shared_inspect` — STAYS on the strict bucket)
- Modify: `tests/unit/web/shareable_reviews/test_routes.py` — `_app_with_share_service` (~lines 61–69) wires only `app.state.rate_limiter`; after the flip, the shareable-link route reads `app.state.write_rate_limiter`, and Starlette `State` raises `AttributeError` on an unset attribute (verified live) — under `ASGITransport(..., raise_app_exceptions=False)` that degrades `test_get_shareable_link_maps_unmarked_current_snapshot_to_409` to a 500. Add `app.state.write_rate_limiter = ComposerRateLimiter(limit=100)` to the fixture.
- Test: `tests/integration/web/test_shareable_reviews_routes.py`

**Interfaces:**
- Consumes: `get_write_rate_limiter` from Task 1.
- Produces: mark-ready POST and shareable-link GET check `write_rate_limiter`; token resolve keeps `rate_limiter`.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/web/test_shareable_reviews_routes.py`, reusing
its existing fixture `audit_readiness_client_with_state: tuple[TestClient, UUID]`
(named in that file's own module docstring):

```python
def test_review_write_endpoints_use_write_bucket_and_resolve_stays_strict(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    client.app.state.rate_limiter = ComposerRateLimiter(limit=1)
    client.app.state.write_rate_limiter = ComposerRateLimiter(limit=100)
    # Two consecutive write-side calls succeed on a starved strict bucket:
    r1 = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert r1.status_code in (200, 409)
    r2 = client.get(f"/api/sessions/{session_id}/shareable-link")
    assert r2.status_code in (200, 409)
    # get_shared_inspect (token resolve) remains strict: the second
    # resolve trips the limit=1 strict bucket.
    token = "not-a-real-token"
    first = client.get(f"/api/sessions/shared/{token}")
    second = client.get(f"/api/sessions/shared/{token}")
    assert 429 in (first.status_code, second.status_code)
```

(Route paths verified against `shareable_reviews/routes.py` lines 54/90/118:
`mark_ready_for_review`, `get_shareable_link`, `get_shared_inspect`.)

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/integration/web/test_shareable_reviews_routes.py -q`; expected: 429 on `r2` (strict bucket shared today).

- [ ] **Step 3: Flip the two write-side dependencies** to `Depends(get_write_rate_limiter)`; leave `get_shared_inspect` untouched; add a one-line comment on each stating the bucket and why (`cheap DB write / token mint` vs `abuse-sensitive token probe stays strict`). Add `app.state.write_rate_limiter = ComposerRateLimiter(limit=100)` to `_app_with_share_service` in `tests/unit/web/shareable_reviews/test_routes.py`.

- [ ] **Step 4: Verify green** — `python -m pytest tests/integration/web/test_shareable_reviews_routes.py tests/unit/web/shareable_reviews/ -q`; expected PASS (the unit sweep is the guard for the fixture this flip breaks — do not narrow it).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/shareable_reviews/routes.py tests/integration/web/test_shareable_reviews_routes.py tests/unit/web/shareable_reviews/test_routes.py
git commit -m "fix(web): meter shareable-review writes on the write bucket; token resolve stays strict"
git log -1
```

---

### Task 4: Document the new env name on the non-terraform deploy surfaces

**Scope note (reality-checked):** the minimum-image-floor test
(`test_documented_minimum_image_revision_is_the_true_settings_floor`,
`tests/unit/deployment/test_aws_ecs_terraform_package.py`) derives its
required-settings set ONLY from git-tracked `.tf` files under
`deploy/aws-ecs/terraform/`. This task deliberately does NOT touch
terraform or the README "Minimum image revision" paragraph: the new
setting has a safe default (60), the AWS stack is torn down, and — with
`required` unchanged — advancing the README commit would BREAK the
test's minimality assertion. If a future task ships the var through
`deploy/aws-ecs/terraform/modules/scenario/locals.tf`, THAT task owns
the floor/README update.

**Files:**
- Modify: `deploy/linux-systemd/elspeth-web.env.example` (beside `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=10`)
- Modify: `deploy/compose/web-postgres.yaml` (beside the same key)

- [ ] **Step 1: Add the env lines**

In `elspeth-web.env.example`, under the existing composer rate-limit line:

```bash
# Cheap DB-write bucket (preferences PATCH, review mark-ready/link).
# Separate from the composer LLM bucket above. Default 60.
ELSPETH_WEB__WRITE_RATE_LIMIT_PER_MINUTE=60
```

In `web-postgres.yaml` (YAML form): `ELSPETH_WEB__WRITE_RATE_LIMIT_PER_MINUTE: "60"` — copy the quoting style of the sibling `COMPOSER_RATE_LIMIT_PER_MINUTE` line exactly.

- [ ] **Step 2: Verify the floor test still passes untouched**

Run: `python -m pytest tests/unit/deployment/ -k minimum_image_revision -q`
Expected: PASS — this task must not move the floor. If it FAILS, stop:
something outside this plan changed terraform; surface it, don't fix it here.

- [ ] **Step 3: Commit**

```bash
git add deploy/linux-systemd/elspeth-web.env.example deploy/compose/web-postgres.yaml
git commit -m "docs(deploy): document ELSPETH_WEB__WRITE_RATE_LIMIT_PER_MINUTE on the env-file surfaces"
git log -1
```

---

### Task 5: Frontend — lift `retry_after` into ApiError

**Files:**
- Modify: `src/elspeth/web/frontend/src/types/index.ts` (`interface ApiError`, ~line 1092)
- Modify: `src/elspeth/web/frontend/src/api/client.ts` (`parseResponse`, envelope extraction ~lines 206–410)
- Test: `src/elspeth/web/frontend/src/api/client.preferences.test.ts`

**Interfaces:**
- Produces: `ApiError.retry_after?: number` — seconds, present only on `rate_limited` envelopes; consumed by Task 6.

- [ ] **Step 1: Write the failing test**

**Wire-shape fact (reality-checked, load-bearing):** the limiter raises
`HTTPException(detail={"error_type": ..., "detail": ..., "retry_after": ...})`
(`rate_limit.py:128-136`), and `handle_http_exception` (`app.py:~1717-1739`)
delegates to FastAPI's default renderer — so the REAL wire body is
**nested**: `{"detail": {"error_type": ..., "detail": ..., "retry_after": 26}}`.
A flat-body mock would green a broken extraction. `parseResponse` already
handles exactly this dual-site case for `timeout_seconds` via
`firstDefined(ownField(body, "timeout_seconds"), ownField(nestedDetail, "timeout_seconds"))`
(`client.ts:~271-282`) — mirror that idiom.

In `client.preferences.test.ts`, following that file's documented
convention (`vi.spyOn(globalThis, "fetch")` + `mockResolvedValueOnce(new Response(...))` —
there is NO `mockFetchOnce` helper in this repo; read the file's existing
tests and reuse its exact spy setup):

```typescript
it("lifts retry_after from a rate-limited 429 envelope into the thrown ApiError", async () => {
  // REAL wire shape: FastAPI renders the limiter's dict detail NESTED
  // under "detail". A flat mock here would falsely pass a flat-only read.
  fetchSpy.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        detail: {
          error_type: "rate_limited",
          detail: "Rate limit exceeded. Try again in 26 seconds.",
          retry_after: 26,
        },
      }),
      { status: 429, headers: { "Content-Type": "application/json" } },
    ),
  );
  await expect(updateUserComposerPreferences({})).rejects.toMatchObject({
    status: 429,
    error_type: "rate_limited",
    retry_after: 26,
  });
});
```

(`fetchSpy` = whatever the file's existing beforeEach names its
`vi.spyOn(globalThis, "fetch")` handle — reuse it, don't create a second spy.)

- [ ] **Step 2: Verify it fails** — `cd src/elspeth/web/frontend && npx vitest run src/api/client.preferences.test.ts`; expected: `retry_after` is `undefined`.

- [ ] **Step 3: Implement** — add `retry_after?: number;` to `ApiError` with a docstring ("Seconds until the per-user rate-limit window frees a slot; present only on `rate_limited` envelopes. Drives the single delayed retry in preferencesStore.markTutorialGraduated."); in `parseResponse`, extract it EXACTLY like `timeout_seconds` (the established dual-site idiom at `client.ts:~271-282`):

```typescript
      const rawRetryAfter = firstDefined(
        ownField(body, "retry_after"),
        ownField(nestedDetail, "retry_after"),
      );
      retryAfter = typeof rawRetryAfter === "number" ? rawRetryAfter : undefined;
```

declare `let retryAfter: number | undefined;` beside the other envelope
locals and add `retry_after: retryAfter,` to the constructed `error`
object. Match the surrounding code's exact helper names — if the
`timeout_seconds` block spells them differently, copy ITS spelling.

- [ ] **Step 4: Verify green** — same vitest command; expected PASS. Also run `npx vitest run src/api/` to catch envelope-shape snapshot tests.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/types/index.ts src/elspeth/web/frontend/src/api/client.ts src/elspeth/web/frontend/src/api/client.preferences.test.ts
git commit -m "feat(web): surface rate-limit retry_after in the ApiError envelope"
git log -1
```

---

### Task 6: Frontend — markTutorialGraduated retries once after a 429

**Files:**
- Modify: `src/elspeth/web/frontend/src/stores/preferencesStore.ts` (`markTutorialGraduated`, ~lines 325–396; its options type takes `via?: "exit"` ONLY — a plain no-args call IS the first-time completion, there is no `"first_time"` value)
- Test: `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts` (mock handle at line ~32: `mockUpdate = vi.mocked(updateUserComposerPreferences)`)

**Interfaces:**
- Consumes: `ApiError.retry_after` from Task 5.
- Produces: unchanged store API; new behavior only.

- [ ] **Step 1: Write the failing tests**

Use the test file's existing module-level mock handle `mockUpdate`
(line ~32) and its existing completed-payload fixture (read the file;
reuse whatever fixture its current graduation tests pass to
`mockUpdate.mockResolvedValueOnce`):

```typescript
it("retries the graduation PATCH once after a rate-limited failure", async () => {
  vi.useFakeTimers();
  mockUpdate
    .mockRejectedValueOnce({ status: 429, error_type: "rate_limited", detail: "…", retry_after: 2 })
    .mockResolvedValueOnce(completedPayload); // reuse the file's payload fixture
  const promise = usePreferencesStore.getState().markTutorialGraduated();
  await vi.advanceTimersByTimeAsync(2_000);
  await expect(promise).resolves.toBe(completedPayload.tutorial_completed_at);
  expect(mockUpdate).toHaveBeenCalledTimes(2);
  expect(usePreferencesStore.getState().writeError).toBeNull();
  vi.useRealTimers();
});

it("gives up after the second rate-limited failure and surfaces the detail", async () => {
  vi.useFakeTimers();
  mockUpdate.mockRejectedValue({
    status: 429, error_type: "rate_limited",
    detail: "Rate limit exceeded. Try again in 2 seconds.", retry_after: 2,
  });
  const promise = usePreferencesStore.getState().markTutorialGraduated();
  promise.catch(() => undefined); // assertion happens via store state below
  await vi.advanceTimersByTimeAsync(2_000);
  await expect(promise).rejects.toMatchObject({ status: 429 });
  expect(mockUpdate).toHaveBeenCalledTimes(2); // exactly one retry, no loop
  expect(usePreferencesStore.getState().writeError).toContain("Rate limit exceeded");
  expect(usePreferencesStore.getState().writing).toBe(false);
  vi.useRealTimers();
});
```

- [ ] **Step 2: Verify both fail** — `cd src/elspeth/web/frontend && npx vitest run src/stores/preferencesStore.test.ts`; expected: first test rejects without retry (1 call, not 2); second test's `writeError` lacks the detail (today a thrown plain-object ApiError hits the non-`Error` branch and produces the bare "Couldn't save tutorial completion.").

- [ ] **Step 3: Implement**

In `preferencesStore.ts`, add a module-local guard (same per-module
pattern as `RunOutputsPanel.tsx` / `shareableReviewStore.ts`):

```typescript
function isRateLimitedApiError(
  err: unknown,
): err is { status: number; detail: string; retry_after?: number } {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { status?: unknown }).status === 429
  );
}

const MAX_RETRY_AFTER_WAIT_MS = 30_000;
```

In `markTutorialGraduated`, wrap the existing
`updateUserComposerPreferences` call in a small once-retrying helper —
keep `writing: true` across the wait so the graduation card's busy state
("Saving tutorial completion") stays honest:

```typescript
      let payload: UserComposerPreferencesPayload;
      try {
        payload = await updateUserComposerPreferences(patchBody);
      } catch (err) {
        // One delayed retry for a rate-limited save: the tutorial's own
        // stage-persist burst can transiently exhaust the write bucket,
        // and completion is the one write that must not be dropped (it
        // gates whether the tutorial re-shows on next load).
        if (!isRateLimitedApiError(err) || err.retry_after === undefined) {
          throw err;
        }
        const waitMs = Math.min(err.retry_after * 1000, MAX_RETRY_AFTER_WAIT_MS);
        await new Promise((resolve) => setTimeout(resolve, waitMs));
        payload = await updateUserComposerPreferences(patchBody);
      }
```

(`patchBody` = the object currently passed inline; hoist it to a const
above the try. The surrounding optimistic-set / rollback structure is
already correct — only the awaited call changes.)

In the catch's `writeError` derivation, surface the ApiError detail
(plain thrown objects are not `instanceof Error`):

```typescript
        writeError:
          err instanceof Error
            ? `Couldn't save tutorial completion: ${err.message}`
            : typeof err === "object" && err !== null && typeof (err as { detail?: unknown }).detail === "string"
              ? `Couldn't save tutorial completion: ${(err as { detail: string }).detail}`
              : "Couldn't save tutorial completion.",
```

- [ ] **Step 4: Verify green** — `npx vitest run src/stores/preferencesStore.test.ts`, then the store's dependents: `npx vitest run src/components/tutorial/`; expected PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/stores/preferencesStore.ts src/elspeth/web/frontend/src/stores/preferencesStore.test.ts
git commit -m "fix(web): retry the tutorial-completion save once after a rate-limit 429 and surface the envelope detail"
git log -1
```

---

### Task 7: Whole-tree verification + live redeploy

**Files:** none (verification only) — except `deploy/elspeth-web.env` if the operator wants a non-default live value.

- [ ] **Step 1: Full backend suite** — `source .venv/bin/activate && python -m pytest tests/ -n 12 -q`; expected: same pass/fail set as before this branch (compare against a pre-change run; the trust-tier corpus gate is EXPECTED to stay red — diff the corpus, add nothing).
- [ ] **Step 2: Full frontend suite** — `cd src/elspeth/web/frontend && npx vitest run`; expected PASS.
- [ ] **Step 3: Live redeploy** (operator-visible; the live host serves elspeth.foundryside.dev):

```bash
cd src/elspeth/web/frontend && npm run build
sudo systemctl restart elspeth-web.service
sleep 3 && systemctl is-active elspeth-web.service && journalctl -u elspeth-web.service -n 20 --no-pager
```

Known traps (memory: reference_live_redeploy_and_mcp_browser_traps): `is-active` lies under a crash-loop — read the journal, don't trust the one-word status; a restart can reveal a masked stale Landscape epoch (rotate `audit.db`+`wal`+`shm` if it does, never `rm`); browsers may serve a stale `index.html` — hard-reload before judging the frontend change.

- [ ] **Step 4: Live probe** — replay the incident shape: 5 rapid `PATCH /api/composer-preferences` with a session cookie while the strict bucket is warm, confirm none 429 (write bucket at 60), and confirm a compose POST still 429s at the 11th call in a minute (strict bucket intact). Watch `journalctl -u elspeth-web.service -f` for the `http_error_envelope` 429 lines.
- [ ] **Step 5: Update memory/tracker** — record the outcome in the filigree issue for this work (create one under the release milestone if none exists) and close it with the verifying evidence.

## Non-goals (deliberate)

- **No debounce/coalesce of tutorial stage persists.** At 60/min the observed worst case (~5 writes/30 s) has 6× headroom; coalescing adds resume-loss risk for no measured need (YAGNI).
- **No Redis/shared-store limiter.** Single-worker deployment is enforced at startup (`app.py` multi-worker check); cross-process accuracy stays out of scope.
- **No generic retry-all-429s in `parseResponse`.** Only the completion save retries: it is the one write whose loss has a durable consequence (tutorial re-shows). Blanket retries would mask real overload and double-fire non-idempotent writes.
