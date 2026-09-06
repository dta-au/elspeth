import { beforeEach, describe, expect, it } from "vitest";
import {
  SSO_CALLBACK_HASH_PREFIX,
  SSO_FAILURE_MESSAGES,
  SSO_GENERIC_FAILURE,
  captureSsoCallback,
  parseSsoCallbackHash,
  ssoFailureMessage,
} from "./ssoCallback";

const CODE = "Q".repeat(43); // token_urlsafe(32) is 43 characters

describe("parseSsoCallbackHash", () => {
  it("is null off the route, so the page behaves as if nothing happened", () => {
    expect(parseSsoCallbackHash("")).toBeNull();
    expect(parseSsoCallbackHash("#/shared/tk-abc")).toBeNull();
    expect(parseSsoCallbackHash("#/session-1/inspect")).toBeNull();
    expect(parseSsoCallbackHash("#/auth/callbackx?code=" + CODE)).toBeNull();
    expect(parseSsoCallbackHash("#/auth/callback/extra?code=" + CODE)).toBeNull();
  });

  it("reads exactly one well-formed handoff code", () => {
    expect(parseSsoCallbackHash(`${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}`)).toEqual({
      kind: "code",
      code: CODE,
    });
  });

  it("reads exactly one known failure category", () => {
    for (const category of Object.keys(SSO_FAILURE_MESSAGES)) {
      expect(parseSsoCallbackHash(`${SSO_CALLBACK_HASH_PREFIX}?error=${category}`)).toEqual({
        kind: "error",
        category,
      });
    }
  });

  it.each([
    ["bare route", SSO_CALLBACK_HASH_PREFIX],
    ["empty query", `${SSO_CALLBACK_HASH_PREFIX}?`],
    ["empty code", `${SSO_CALLBACK_HASH_PREFIX}?code=`],
    ["duplicate code", `${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}&code=${CODE}`],
    ["code and error together", `${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}&error=sso_idp_error`],
    ["code outside the token_urlsafe alphabet", `${SSO_CALLBACK_HASH_PREFIX}?code=${"Q".repeat(20)}%2F${"Q".repeat(20)}`],
    ["code beyond the backend's 128 bound", `${SSO_CALLBACK_HASH_PREFIX}?code=${"Q".repeat(129)}`],
    ["unknown category", `${SSO_CALLBACK_HASH_PREFIX}?error=access_denied`],
    ["category with IdP text riding along", `${SSO_CALLBACK_HASH_PREFIX}?error=sso_idp_error&error_description=secret`],
    ["a token where a code should be", `${SSO_CALLBACK_HASH_PREFIX}?access_token=secret`],
    ["an extra parameter", `${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}&state=x`],
    ["oversized", `${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}&` + "x".repeat(64 * 1024)],
  ])("refuses %s as malformed rather than adopting part of it", (_name, hash) => {
    expect(parseSsoCallbackHash(hash)).toEqual({ kind: "malformed" });
  });
});

describe("captureSsoCallback", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("scrubs the fragment synchronously and keeps the path and query", () => {
    window.history.replaceState(null, "", `/app?verify_token=x${SSO_CALLBACK_HASH_PREFIX}?code=${CODE}`);
    expect(captureSsoCallback()).toEqual({ kind: "code", code: CODE });
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/app?verify_token=x");
  });

  it("scrubs a malformed callback too — it must not survive a reload", () => {
    window.history.replaceState(null, "", `/${SSO_CALLBACK_HASH_PREFIX}?access_token=secret`);
    expect(captureSsoCallback()).toEqual({ kind: "malformed" });
    expect(window.location.hash).toBe("");
  });

  it("leaves every other hash alone", () => {
    window.history.replaceState(null, "", "/#/shared/tk-abc");
    expect(captureSsoCallback()).toBeNull();
    expect(window.location.hash).toBe("#/shared/tk-abc");
  });
});

describe("ssoFailureMessage", () => {
  it("names what the person can act on and nothing an attacker can learn from", () => {
    expect(ssoFailureMessage({ kind: "error", category: "sso_access_pending" })).toMatch(/awaiting approval/);
    expect(ssoFailureMessage({ kind: "error", category: "sso_identity_disabled" })).toMatch(/disabled/);
    expect(ssoFailureMessage({ kind: "error", category: "provider_unavailable" })).toMatch(/unavailable/);
    for (const category of ["sso_state_mismatch", "sso_id_token_invalid", "sso_handoff_invalid"] as const) {
      expect(ssoFailureMessage({ kind: "error", category })).toBe(SSO_GENERIC_FAILURE);
    }
    expect(ssoFailureMessage({ kind: "malformed" })).toBe(SSO_GENERIC_FAILURE);
  });

  it("has a sentence for every category, none of which echoes the category", () => {
    for (const [category, message] of Object.entries(SSO_FAILURE_MESSAGES)) {
      expect(message.length).toBeGreaterThan(0);
      expect(message).not.toContain(category);
    }
  });
});
