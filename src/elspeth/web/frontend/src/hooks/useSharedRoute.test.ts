import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSharedRoute } from "./useSharedRoute";
import { parseSharedRoute } from "@/utils/hashRoutes";

describe("parseSharedRoute", () => {
  it.each(["", "#/sess-uuid", "#composer-main", "#/shared-other"])(
    "leaves ordinary navigation %s to its owner", (hash) => {
      expect(parseSharedRoute(hash)).toEqual({ kind: "none" });
    },
  );
  it.each(["#/shared", "#/shared/", "#/shared/abc%ZZ"])(
    "keeps malformed shared route %s out of Composer", (hash) => {
      expect(parseSharedRoute(hash)).toEqual({ kind: "invalid" });
    },
  );
  it("extracts and decodes opaque tokens without interpreting their authority", () => {
    expect(parseSharedRoute("#/shared/abc%2Fdef%2B")).toEqual({ kind: "valid", token: "abc/def+" });
  });
});

describe("useSharedRoute", () => {
  beforeEach(() => window.history.replaceState(null, "", "/"));

  it("reads shared route ownership on mount", () => {
    window.history.replaceState(null, "", "#/shared/initial-token");
    const { result } = renderHook(() => useSharedRoute());
    expect(result.current).toEqual({ kind: "valid", token: "initial-token" });
  });

  it.each(["hashchange", "popstate"])("tracks valid, malformed and ordinary arrivals through %s", (event) => {
    const { result } = renderHook(() => useSharedRoute());
    expect(result.current).toEqual({ kind: "none" });
    for (const hash of ["#/shared/token", "#/shared/", "#/some-session"]) {
      act(() => {
        window.history.replaceState(null, "", hash);
        window.dispatchEvent(new Event(event));
      });
      expect(result.current).toEqual(parseSharedRoute(hash));
    }
  });
});
