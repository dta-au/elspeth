/**
 * Tests for the account-level user-composer-preferences API helpers
 * (Phase 1B Task 1).
 *
 * Convention: vi.spyOn(globalThis, "fetch"). Spying on the real fetch exercises the real
 * authHeaders() / parseResponse<T>() pipeline (including the 401-logout
 * interceptor and the FastAPI envelope decode); a module-level vi.mock
 * would stub those out and leave them uncovered.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fetchUserComposerPreferences,
  updateUserComposerPreferences,
} from "./client";
import type { UserComposerPreferencesPayload } from "@/types/api";

function makePayload(
  overrides: Partial<UserComposerPreferencesPayload> = {},
): UserComposerPreferencesPayload {
  return {
    freeform_intro_dismissed_at: null,
    tutorial_completed_at: null,
    tutorial_stage: null,
    tutorial_session_id: null,
    tutorial_run_id: null,
    tutorial_source_data_hash: null,
    show_advanced: false,
    updated_at: "2026-05-16T00:00:00Z",
    ...overrides,
  };
}

describe("api/client user composer preferences", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("GET parses the UserComposerPreferences payload", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify(makePayload()), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const prefs = await fetchUserComposerPreferences();

    expect(prefs.show_advanced).toBe(false);
    expect(prefs.tutorial_completed_at).toBeNull();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/composer-preferences");
    expect(init?.method).toBeUndefined();
  });

  it("PATCH sends only the supplied partial fields", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify(makePayload({ show_advanced: true })), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const result = await updateUserComposerPreferences({
      show_advanced: true,
    });

    expect(result.show_advanced).toBe(true);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/composer-preferences");
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(init?.body as string)).toEqual({
      show_advanced: true,
    });
  });

  it("GET throws an ApiError on non-2xx (5xx server failure)", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        statusText: "Internal Server Error",
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchUserComposerPreferences()).rejects.toMatchObject({
      status: 500,
    });
  });

  it("PATCH throws an ApiError on 422 (invalid preference)", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "invalid preference" }), {
        status: 422,
        statusText: "Unprocessable Entity",
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(
      // @ts-expect-error -- intentionally invalid field type to exercise the 422 branch
      updateUserComposerPreferences({ show_advanced: "yes" }),
    ).rejects.toMatchObject({ status: 422 });
  });
  it("PATCH sends the tutorial resume fields when supplied (elspeth-918f4434b3)", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(
        JSON.stringify(
          makePayload({
            tutorial_stage: "build",
            tutorial_session_id: "sess-1",
          }),
        ),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const result = await updateUserComposerPreferences({
      tutorial_stage: "build",
      tutorial_session_id: "sess-1",
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    });

    expect(result.tutorial_stage).toBe("build");
    expect(result.tutorial_session_id).toBe("sess-1");
    const [, init] = fetchSpy.mock.calls[0];
    expect(JSON.parse(init?.body as string)).toEqual({
      tutorial_stage: "build",
      tutorial_session_id: "sess-1",
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    });
  });

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
});
