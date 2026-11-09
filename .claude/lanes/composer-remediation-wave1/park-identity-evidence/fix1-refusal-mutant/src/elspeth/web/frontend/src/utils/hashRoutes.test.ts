import { describe, expect, it } from "vitest";
import { isNavigationDestination, parseSessionHash } from "./hashRoutes";

describe("authentication destination admission", () => {
  it.each(["#/shared", "#/shared/", "#/shared/abc%ZZ", "#/shared/token", "#/session-id", "#/session-id/graph"])(
    "preserves owned navigation form %s", (hash) => {
      expect(isNavigationDestination(hash)).toBe(true);
    },
  );
  it.each(["", "#composer-main", "#/auth/callback", "#/auth/callback?code=synthetic", "#/invalid/multiple/segments", "https://example.test/"])(
    "refuses non-destination %s", (hash) => {
      expect(isNavigationDestination(hash)).toBe(false);
      expect(parseSessionHash(hash)).toEqual({ sessionId: null, verb: null });
    },
  );
});
