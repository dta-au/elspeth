import { describe, expect, it } from "vitest";
import * as composer from "@/config/composer";

describe("durable composer observation policy", () => {
  it("adds a fixed grace to the server remaining duration", () => {
    expect(composer.COMPOSE_CLIENT_GRACE_MS).toBe(25_000);
  });
  it("has no stale deployment wall-clock default", () => {
    expect("DEFAULT_COMPOSE_TIMEOUT_MS" in composer).toBe(false);
    expect("getComposeTimeoutMs" in composer).toBe(false);
  });
  it("does not derive mutable admission readiness from health values", () => {
    expect("applyServerComposerTimeout" in composer).toBe(false);
    expect("runComposeWithTimeout" in composer).toBe(false);
  });
  it("limits refresh-only recovery to uncertain or already saved outcomes", () => {
    for (const code of [composer.COMPOSE_OUTCOME_UNCONFIRMED, "recompose_saved_proposal", "recompose_already_completed"]) expect(composer.isComposeRefreshOnlyFailureCode(code)).toBe(true);
    for (const code of [undefined, "request_cancelled", "stale_compose_state", "admission_refused", "token_accounting_unavailable"]) expect(composer.isComposeRefreshOnlyFailureCode(code)).toBe(false);
  });
  it("distinguishes observation detachment from explicit Stop", () => {
    expect(composer.COMPOSE_TIMEOUT_ABORT_REASON).toBe("compose_timeout");
    expect(composer.COMPOSE_USER_CANCEL_ABORT_REASON).toBe("compose_user_cancel");
    expect(composer.COMPOSE_TIMEOUT_ABORT_REASON).not.toBe(composer.COMPOSE_USER_CANCEL_ABORT_REASON);
  });
  it("names the selected-session loading guard", () => {
    expect(composer.COMPOSE_LOADING_SESSION_MESSAGE).toBe("Loading this session…");
  });
});
