import { canonicalUuid, exactRecord, operationKind, text } from "@/api/composerOperationDecoder";
import type { OperationCustody, PrincipalScope, SessionCustody, SubmittedCustody } from "@/types/composerOperations";

export const COMPOSER_CUSTODY_KEY = "elspeth_composer_operations_v1";
export const COMPOSER_BODY_LIMIT = 512 * 1024;
export const COMPOSER_AGGREGATE_LIMIT = 1024 * 1024;
export const COMPOSER_CUSTODY_LIFETIME_MS = 24 * 60 * 60 * 1000;
export const COMPOSER_STOP_KEY = "elspeth_composer_stop_controls_v1";
const stopped = new Set<string>();
let stopPersistenceLimited = false;
let stopControlsQuarantined = false;
export function composerStopPersistenceLimited(): boolean { return stopPersistenceLimited; }
let expiredHydration = false;
let quarantinedHydration = false;
const schema = "composer-operations.v1";
const encoder = new TextEncoder();
const records = new Map<string, SessionCustody>();
let authenticatedScope: PrincipalScope | null = null;
let hydrated = false;
class CustodyPrincipalMismatch extends Error {}
export function samePrincipal(a: PrincipalScope, b: PrincipalScope): boolean { return a.principalId === b.principalId && a.authProvider === b.authProvider; }
function key(scope: PrincipalScope, sessionId: string): string { return JSON.stringify([scope.principalId, scope.authProvider, sessionId]); }
function bytes(value: unknown): number { return encoder.encode(JSON.stringify(value)).byteLength; }
function admitDescriptor(value: unknown, scope: PrincipalScope, restore = false): OperationCustody {
  const record = exactRecord(value, ["mode", "scope", "sessionId", "operationId", "kind", "createdAt", "body"], ["mode", "scope", "sessionId", "operationId", "kind", "createdAt"]);
  const persistedScope = exactRecord(record.scope, ["principalId", "authProvider"]);
  if (persistedScope.principalId !== scope.principalId || persistedScope.authProvider !== scope.authProvider) throw new CustodyPrincipalMismatch("Custody principal mismatch");
  const sessionId = canonicalUuid(record.sessionId), operationId = canonicalUuid(record.operationId), kind = operationKind(record.kind);
  if (typeof record.createdAt !== "number" || !Number.isSafeInteger(record.createdAt) || record.createdAt < 0) throw new Error("Invalid custody timestamp");
  if (record.createdAt > Date.now() || Date.now() - record.createdAt > COMPOSER_CUSTODY_LIFETIME_MS) {
    if (!restore) throw new Error("Expired custody requires reconciliation");
    expiredHydration = true;
  }
  const common = { scope: Object.freeze({ ...scope }), sessionId, operationId, kind, createdAt: record.createdAt };
  if (record.mode === "observer") {
    if ("body" in record) throw new Error("Observer cannot possess replay body");
    return Object.freeze({ ...common, mode: "observer" });
  }
  if (record.mode !== "submitted") throw new Error("Unknown custody mode");
  const body = exactRecord(record.body, kind === "compose_message" ? ["operation_id", "content", "state_id"] : ["operation_id", "expected_user_message_id", "state_id"]);
  if (canonicalUuid(body.operation_id) !== operationId) throw new Error("Custody body identity mismatch");
  const state_id = body.state_id === null ? null : canonicalUuid(body.state_id);
  if (bytes(body) > COMPOSER_BODY_LIMIT) throw new Error("Composer body exceeds custody limit");
  if (kind === "compose_message") {
    const content = text(body.content);
    if (content.trim() === "" || [...content].length > 65536) throw new Error("Invalid custody content");
    return Object.freeze({ ...common, mode: "submitted", kind, body: Object.freeze({ operation_id: operationId, content, state_id }) });
  }
  return Object.freeze({ ...common, mode: "submitted", kind, body: Object.freeze({ operation_id: operationId, expected_user_message_id: canonicalUuid(body.expected_user_message_id), state_id }) });
}
function serializedRecords(source: ReadonlyMap<string, SessionCustody>): string {
  const entries = [...source.values()].map((record) => ({ foreground: record.foreground, unresolvedSubmissions: [...record.unresolvedSubmissions.values()] }));
  return JSON.stringify({ schema, entries });
}
function reservedCustodyBytes(source: ReadonlyMap<string, SessionCustody>): number {
  const identities: string[] = [];
  const known = new Set<string>();
  let attachmentReserve = 0;
  for (const record of source.values()) {
    const descriptors = [...record.unresolvedSubmissions.values(), ...(record.foreground === null ? [] : [record.foreground])];
    for (const descriptor of descriptors) {
      const identity = JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]);
      known.add(identity); identities.push(identity);
      if (descriptor === record.foreground && descriptor.mode === "submitted") {
        // A refused ambiguous submission can acquire one distinct observer.
        identities.push(identity);
        attachmentReserve += bytes({ ...descriptor, mode: "observer", body: undefined });
      }
    }
  }
  for (const identity of stopped) if (!known.has(identity)) identities.push(identity);
  return encoder.encode(serializedRecords(source)).byteLength + bytes(identities) + attachmentReserve;
}
function persistStops(): void {
  if (stopControlsQuarantined) { stopPersistenceLimited = true; return; }
  const raw = JSON.stringify([...stopped]);
  let heldBytes = encoder.encode(serializedRecords(records)).byteLength;
  if (hydrationBlocked) try { heldBytes = Math.max(heldBytes, encoder.encode(sessionStorage.getItem(COMPOSER_CUSTODY_KEY) ?? "").byteLength); } catch { /* Memory-only custody. */ }
  if (encoder.encode(raw).byteLength + heldBytes > COMPOSER_AGGREGATE_LIMIT) { stopPersistenceLimited = true; return; }
  try { sessionStorage.setItem(COMPOSER_STOP_KEY, raw); stopPersistenceLimited = false; } catch { stopPersistenceLimited = true; /* Storage denial retains exact same-tab intent. */ }
}
function persist(): void {
  if (hydrationBlocked) return;
  try { sessionStorage.setItem(COMPOSER_CUSTODY_KEY, serializedRecords(records)); } catch { /* Memory custody remains authoritative in this tab. */ }
}
export function authenticateComposerCustody(scope: PrincipalScope): void {
  if (typeof scope.principalId !== "string" || scope.principalId === "" || !["local", "oidc", "entra", "vanguard", "google"].includes(scope.authProvider)) throw new Error("Invalid authenticated principal/provider scope");
  if (authenticatedScope !== null && !samePrincipal(authenticatedScope, scope)) purgeComposerCustody();
  authenticatedScope = Object.freeze({ ...scope });
  try {
    const rawControls = sessionStorage.getItem(COMPOSER_STOP_KEY);
    if (rawControls !== null) {
      if (encoder.encode(rawControls).byteLength > COMPOSER_AGGREGATE_LIMIT) throw new Error("Oversized Stop controls");
      const controls: unknown = JSON.parse(rawControls);
      if (!Array.isArray(controls)) throw new Error("Invalid Stop controls");
      for (const value of controls) try {
        if (typeof value !== "string") throw new Error("Invalid Stop identity");
        const identity: unknown = JSON.parse(value);
        if (!Array.isArray(identity) || identity.length !== 4 || JSON.stringify(identity) !== value) throw new Error("Invalid Stop identity");
        if (identity[0] !== scope.principalId || identity[1] !== scope.authProvider) continue;
        canonicalUuid(identity[2]); canonicalUuid(identity[3]); stopped.add(value);
      } catch { stopControlsQuarantined = true; stopPersistenceLimited = true; }
    }
  } catch { stopControlsQuarantined = true; stopPersistenceLimited = true; }

  if (hydrated) return;
  hydrated = true;
  let raw: string | null;
  try { raw = sessionStorage.getItem(COMPOSER_CUSTODY_KEY); } catch { return; }
  if (raw === null) return;
  try {
    if (encoder.encode(raw).byteLength > COMPOSER_AGGREGATE_LIMIT) throw new Error("Oversized custody storage");
    const root = exactRecord(JSON.parse(raw), ["schema", "entries"]);
    if (root.schema !== schema || !Array.isArray(root.entries)) throw new Error("Invalid custody schema");
    const restored = new Map<string, SessionCustody>();
    for (const value of root.entries) {
      try {
      const record = exactRecord(value, ["foreground", "unresolvedSubmissions"]);
      if (!Array.isArray(record.unresolvedSubmissions)) throw new Error("Invalid unresolved submissions");
      const foreground = record.foreground === null ? null : admitDescriptor(record.foreground, scope, true);
      const unresolved = new Map<string, SubmittedCustody>();
      for (const item of record.unresolvedSubmissions) {
        const descriptor = admitDescriptor(item, scope, true);
        if (descriptor.mode !== "submitted") throw new Error("Unresolved observer");
        if (foreground !== null && descriptor.sessionId !== foreground.sessionId) throw new Error("Custody session mismatch");
        if (unresolved.has(descriptor.operationId) || foreground?.operationId === descriptor.operationId) throw new Error("Duplicate custody");
        unresolved.set(descriptor.operationId, descriptor);
      }
      const sessionId = foreground?.sessionId ?? [...unresolved.values()][0]?.sessionId;
      if (sessionId === undefined || [...unresolved.values()].some((item) => item.sessionId !== sessionId)) throw new Error("Invalid custody session");
      const entryKey = key(scope, sessionId);
      if (restored.has(entryKey)) throw new Error("Duplicate custody session");
      restored.set(entryKey, { foreground, unresolvedSubmissions: unresolved });
      } catch (error) {
        if (error instanceof CustodyPrincipalMismatch) throw error;
        hydrationBlocked = true; quarantinedHydration = true;
      }
    }
    let total = 0;
    for (const record of restored.values()) {
      if (record.foreground?.mode === "submitted") total += bytes(record.foreground.body);
      for (const item of record.unresolvedSubmissions.values()) total += bytes(item.body);
    }
    if (total > COMPOSER_AGGREGATE_LIMIT) throw new Error("Custody aggregate limit");
    for (const [entryKey, record] of restored) records.set(entryKey, record);
    if (expiredHydration) hydrationBlocked = true;
  } catch (error) {
    if (error instanceof CustodyPrincipalMismatch) {
      records.clear(); stopped.clear(); stopControlsQuarantined = false; stopPersistenceLimited = false;
      try { sessionStorage.removeItem(COMPOSER_STOP_KEY); sessionStorage.removeItem(COMPOSER_CUSTODY_KEY); } catch { /* Storage unavailable. */ }
      return;
    }
    // Malformed or expired submitted descriptors cannot authorize replay.
    // Retain their exact storage while refusing further body-bearing admission.
    hydrationBlocked = true; quarantinedHydration = true;
  }
}
let hydrationBlocked = false;
export function composerCustodyScope(): PrincipalScope | null { return authenticatedScope; }
export function findComposerOperationCustody(scope: PrincipalScope, sessionId: string): SessionCustody {
  return records.get(key(scope, sessionId)) ?? { foreground: null, unresolvedSubmissions: new Map() };
}
export function composerCustodySessionIds(scope: PrincipalScope): readonly string[] {
  const sessions = new Set<string>();
  for (const record of records.values()) for (const descriptor of [...record.unresolvedSubmissions.values(), ...(record.foreground === null ? [] : [record.foreground])]) if (samePrincipal(scope, descriptor.scope)) sessions.add(descriptor.sessionId);
  return [...sessions];
}
export function composerCustodyRecoveryNotice(): string | null {
  if (stopControlsQuarantined) return "Stored cancellation data could not be safely read. Unknown records were retained; keep this tab open while known requests are reconciled.";
  if (hydrationBlocked) return "Stored composer requests need read-only recovery. Known requests are being checked; new submissions remain paused until unresolved records are reconciled.";
  return null;
}
export function acquireComposerOperationCustody(descriptor: SubmittedCustody): SubmittedCustody {
  if (authenticatedScope === null || !samePrincipal(authenticatedScope, descriptor.scope) || hydrationBlocked || stopControlsQuarantined) throw new Error("Authenticate and reconcile composer custody before submitting");
  const admitted = admitDescriptor(descriptor, descriptor.scope);
  if (admitted.mode !== "submitted") throw new Error("Submitted custody required");
  const existing = findComposerOperationCustody(descriptor.scope, descriptor.sessionId);
  if (existing.foreground !== null) throw new Error("A composer action already owns this session");
  let total = bytes(admitted.body);
  for (const record of records.values()) {
    if (record.foreground?.mode === "submitted") total += bytes(record.foreground.body);
    for (const item of record.unresolvedSubmissions.values()) total += bytes(item.body);
  }
  if (total > COMPOSER_AGGREGATE_LIMIT) throw new Error("Reconcile pending composer actions before submitting more content");
  const candidate = new Map(records);
  candidate.set(key(descriptor.scope, descriptor.sessionId), { ...existing, foreground: admitted });
  // Reserve every held identity's eventual Stop control and active attachment.
  if (reservedCustodyBytes(candidate) > COMPOSER_AGGREGATE_LIMIT) throw new Error("Reconcile pending composer actions before submitting more content");
  records.set(key(descriptor.scope, descriptor.sessionId), { ...existing, foreground: admitted }); persist(); return admitted;
}
export function attachComposerObserver(descriptor: OperationCustody, activeId: string, kind: unknown, retainAmbiguous: boolean): OperationCustody {
  if (activeId === descriptor.operationId) throw new Error("Active refusal cannot relabel the submitted action");
  const existing = findComposerOperationCustody(descriptor.scope, descriptor.sessionId);
  const unresolved = new Map(existing.unresolvedSubmissions);
  if (retainAmbiguous && descriptor.mode === "submitted") unresolved.set(descriptor.operationId, descriptor);
  const observer = admitDescriptor({ mode: "observer", scope: descriptor.scope, sessionId: descriptor.sessionId, operationId: activeId, kind, createdAt: Date.now() }, descriptor.scope);
  records.set(key(descriptor.scope, descriptor.sessionId), { foreground: observer, unresolvedSubmissions: unresolved }); persist(); return observer;
}
export function composerStopRequested(descriptor: OperationCustody): boolean {
  const identity = JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]);
  if (stopped.has(identity)) return true;
  return false;
}
export function requestComposerStop(descriptor: OperationCustody): void {
  if (authenticatedScope === null || !samePrincipal(authenticatedScope, descriptor.scope)) return;
  stopped.add(JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]));
  persistStops();
}
function clearComposerStop(descriptor: OperationCustody): void {
  composerStopRequested(descriptor);
  stopped.delete(JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]));
  persistStops();
}
export function settleComposerCustody(descriptor: OperationCustody): void {
  clearComposerStop(descriptor);
  const existing = findComposerOperationCustody(descriptor.scope, descriptor.sessionId);
  const unresolved = new Map(existing.unresolvedSubmissions); unresolved.delete(descriptor.operationId);
  const foreground = existing.foreground?.operationId === descriptor.operationId ? null : existing.foreground;
  if (foreground === null && unresolved.size === 0) records.delete(key(descriptor.scope, descriptor.sessionId));
  else records.set(key(descriptor.scope, descriptor.sessionId), { foreground, unresolvedSubmissions: unresolved });
  if (expiredHydration && !quarantinedHydration && records.size === 0) { expiredHydration = false; hydrationBlocked = false; }
  persist();
}
export function clearComposerSessionCustody(scope: PrincipalScope, sessionId: string): void {
  const existing = findComposerOperationCustody(scope, sessionId);
  if (existing.foreground !== null) clearComposerStop(existing.foreground);
  for (const descriptor of existing.unresolvedSubmissions.values()) clearComposerStop(descriptor);
  records.delete(key(scope, sessionId));
  if (expiredHydration && !quarantinedHydration && records.size === 0) { expiredHydration = false; hydrationBlocked = false; }
  persist();
}
export function purgeComposerCustody(): void {
  records.clear(); stopped.clear(); stopPersistenceLimited = false; stopControlsQuarantined = false; expiredHydration = false; quarantinedHydration = false; authenticatedScope = null; hydrated = false; hydrationBlocked = false;
  try { sessionStorage.removeItem(COMPOSER_CUSTODY_KEY); sessionStorage.removeItem(COMPOSER_STOP_KEY); } catch { /* No persisted credentials or fallback token. */ }
}
