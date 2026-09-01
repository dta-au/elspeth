import { afterEach, describe, expect, it, vi } from "vitest";

import { preferredScrollBehavior } from "./motion";

describe("preferredScrollBehavior", () => {
  const originalMatchMedia = window.matchMedia;

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it("returns 'smooth' when the user expresses no motion preference", () => {
    // The global test stub answers matches:false for every query.
    expect(preferredScrollBehavior()).toBe("smooth");
  });

  it("returns 'auto' when prefers-reduced-motion: reduce matches", () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)",
      media: query,
    }));
    expect(preferredScrollBehavior()).toBe("auto");
  });

  it("consults the live preference on every call, not a cached module-load read", () => {
    const reduced = { value: false };
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)" && reduced.value,
      media: query,
    }));
    expect(preferredScrollBehavior()).toBe("smooth");
    reduced.value = true;
    expect(preferredScrollBehavior()).toBe("auto");
  });

  it("falls back to 'smooth' when matchMedia is unavailable", () => {
    // Same guard shape as useTheme.ts — SSR / stripped test environments.
    window.matchMedia = undefined as unknown as typeof window.matchMedia;
    expect(preferredScrollBehavior()).toBe("smooth");
  });
});
