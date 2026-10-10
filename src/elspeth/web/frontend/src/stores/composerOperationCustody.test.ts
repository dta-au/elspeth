import { beforeEach, describe, expect, it } from "vitest";
import { acquireComposerOperationCustody, attachComposerObserver, authenticateComposerCustody, COMPOSER_BODY_LIMIT, COMPOSER_CUSTODY_KEY, findComposerOperationCustody, purgeComposerCustody, settleComposerCustody } from "./composerOperationCustody";
import type { SubmittedCustody } from "@/types/composerOperations";
const scope = { principalId: "principal-1", authProvider: "local" };
const sid = "11111111-1111-4111-8111-111111111111";
const id = "22222222-2222-4222-8222-222222222222";
const other = "33333333-3333-4333-8333-333333333333";
function descriptor(): SubmittedCustody { return { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null } }; }
beforeEach(() => { purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope); });
describe("immutable principal-scoped durable custody", () => {
  it("freezes the admitted exact body and persists no bearer token", () => {
    const input = descriptor(); const admitted = acquireComposerOperationCustody(input);
    expect(Object.isFrozen(admitted.body)).toBe(true); expect(Object.isFrozen(admitted)).toBe(true);
    expect(admitted.body).toEqual(input.body);
    expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).not.toContain("auth_token");
    expect(() => acquireComposerOperationCustody(descriptor())).toThrow();
  });
  it("never rekeys a losing body when attaching an active observer", () => {
    const admitted = acquireComposerOperationCustody(descriptor());
    const observer = attachComposerObserver(admitted, other, "compose_recompose", true);
    expect(observer.mode).toBe("observer"); expect("body" in observer).toBe(false);
    let record = findComposerOperationCustody(scope, sid);
    expect(record.unresolvedSubmissions.get(id)?.body.operation_id).toBe(id);
    settleComposerCustody(observer);
    record = findComposerOperationCustody(scope, sid);
    expect(record.foreground).toBeNull(); expect(record.unresolvedSubmissions.has(id)).toBe(true);
    settleComposerCustody(admitted); expect(findComposerOperationCustody(scope, sid).unresolvedSubmissions.size).toBe(0);
  });
  it("purges cross-principal replacement and refuses cross-scope admission", () => {
    acquireComposerOperationCustody(descriptor()); authenticateComposerCustody({ ...scope, principalId: "other" });
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBeNull();
    expect(() => acquireComposerOperationCustody(descriptor())).toThrow();
  });
  it("measures the worst supported escaped body below the proposed cap", () => {
    const input = descriptor();
    const content = "\u0001".repeat(65536);
    const body = { ...input.body, content };
    const measured = new TextEncoder().encode(JSON.stringify(body)).byteLength;
    expect(measured).toBe(393300); expect(measured).toBeLessThan(COMPOSER_BODY_LIMIT);
    expect(acquireComposerOperationCustody({ ...input, kind: "compose_message", body }).body).toEqual(body);
    console.info(`custody escaped-body bytes=${measured}, cap=${COMPOSER_BODY_LIMIT}`);
  });
  it("hydrates only freshly authenticated same-scope immutable descriptors", () => {
    const submitted = descriptor();
    purgeComposerCustody();
    sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({ schema: "composer-operations.v1", entries: [{ foreground: submitted, unresolvedSubmissions: [] }] }));
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    authenticateComposerCustody(scope);
    const restored = findComposerOperationCustody(scope, sid).foreground;
    expect(restored).toEqual(submitted); expect(restored !== null && restored.mode === "submitted" && Object.isFrozen(restored.body)).toBe(true);
    authenticateComposerCustody(scope); expect(findComposerOperationCustody(scope, sid).foreground).toBe(restored);
  });
  it("refuses new admission rather than evicting unresolved bodies at the aggregate cap", () => {
    const body = { operation_id: id, content: "\u0001".repeat(65536), state_id: null };
    const first = acquireComposerOperationCustody({ ...descriptor(), kind: "compose_message", body });
    attachComposerObserver(first, other, "compose_message", true);
    const secondId = "44444444-4444-4444-8444-444444444444";
    acquireComposerOperationCustody({ ...descriptor(), sessionId: other, operationId: secondId, kind: "compose_message", body: { ...body, operation_id: secondId } });
    const thirdId = "55555555-5555-4555-8555-555555555555";
    expect(() => acquireComposerOperationCustody({ ...descriptor(), sessionId: thirdId, operationId: thirdId, kind: "compose_message", body: { ...body, operation_id: thirdId } })).toThrow();
    expect(findComposerOperationCustody(scope, sid).unresolvedSubmissions.get(id)?.body).toEqual(body);
  });
  it("purges persisted old-principal bodies on a freshly authenticated reload", () => {
    const old = descriptor(); purgeComposerCustody();
    sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({ schema: "composer-operations.v1", entries: [{ foreground: old, unresolvedSubmissions: [] }] }));
    authenticateComposerCustody({ principalId: "other", authProvider: "local" });
    expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBeNull(); expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("rejects persisted observer bodies and mismatched submitted identities", () => {
    purgeComposerCustody();
    sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({ schema: "composer-operations.v1", entries: [{ foreground: { ...descriptor(), mode: "observer" }, unresolvedSubmissions: [] }] }));
    authenticateComposerCustody(scope); expect(findComposerOperationCustody(scope, sid).foreground).toBeNull(); expect(() => acquireComposerOperationCustody(descriptor())).toThrow();
    purgeComposerCustody(); authenticateComposerCustody(scope);
    expect(() => acquireComposerOperationCustody({ ...descriptor(), kind: "compose_message", body: { operation_id: other, content: "hello", state_id: null } })).toThrow();
  });
});
