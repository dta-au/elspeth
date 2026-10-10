import * as api from "./client";
import { currentAuthGeneration, isCurrentAuthGeneration } from "./authSession";
import { ComposerSseDecoder, validateComposerRecoveryBody, decodeComposerOperationFrame } from "./composerOperationDecoder";
import { COMPOSER_CUSTODY_LIFETIME_MS, acquireComposerOperationCustody, composerStopRequested, requestComposerStop, attachComposerObserver, clearComposerSessionCustody, composerCustodyScope, findComposerOperationCustody, samePrincipal, settleComposerCustody } from "@/stores/composerOperationCustody";
import { COMPOSE_CLIENT_GRACE_MS, COMPOSE_USER_CANCEL_ABORT_REASON } from "@/config/composer";
import type { ComposerProgressSnapshot, MessageWithStateResponse } from "@/types/index";
import type { OperationCustody, SubmittedCustody } from "@/types/composerOperations";

export class ComposerObservationDetached extends Error { constructor() { super("Composer observation detached; the durable action remains pending"); this.name = "ComposerObservationDetached"; } }
export class ComposerSessionMissing extends Error { constructor() { super("Session not found"); this.name = "ComposerSessionMissing"; } }
export class ComposerActiveAttachment extends Error { constructor() { super("Another composer action is active. Your unsent content remains a draft."); this.name = "ComposerActiveAttachment"; } }
interface ObserverOptions { signal?: AbortSignal; current?: () => boolean; progress?: (snapshot: ComposerProgressSnapshot) => void; timeout?: () => void; reconciliationOnly?: boolean }
interface ObserverOwner { generation: number; descriptor: OperationCustody; controller: AbortController; stop: boolean; stopRequested: boolean; stopTarget: OperationCustody | null }
const owners = new Map<string, ObserverOwner>();
function replayAgeEligible(descriptor: OperationCustody): boolean {
  const age = Date.now() - descriptor.createdAt;
  return age >= 0 && age <= COMPOSER_CUSTODY_LIFETIME_MS;
}
function ownerKey(descriptor: OperationCustody): string { return JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]); }
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new ComposerObservationDetached()); return; }
    const abort = () => { clearTimeout(timer); reject(new ComposerObservationDetached()); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    signal.addEventListener("abort", abort, { once: true });
  });
}
export function detachComposerObservers(sessionId?: string): void {
  for (const owner of owners.values()) if (sessionId === undefined || owner.descriptor.sessionId === sessionId) owner.controller.abort();
}
export function cancelObservedComposerOperation(sessionId: string): boolean {
  const scope = composerCustodyScope();
  if (scope === null) return false;
  const record = findComposerOperationCustody(scope, sessionId);
  const targets = new Map<string, OperationCustody>(record.unresolvedSubmissions);
  if (record.foreground !== null) targets.set(record.foreground.operationId, record.foreground);
  if (targets.size === 0) return false;
  let allObserved = true;
  for (const target of targets.values()) {
    const owner = [...owners.values()].find((item) => !item.controller.signal.aborted && isCurrentAuthGeneration(item.generation) && item.descriptor.operationId === target.operationId && item.descriptor.sessionId === sessionId && samePrincipal(item.descriptor.scope, scope));
    requestComposerStop(target);
    if (owner !== undefined) { owner.stop = true; owner.stopRequested = true; owner.stopTarget = target; }
    else allObserved = false;
    // Only durable terminal establishes settlement. Capture each held target,
    // including unresolved admissions, without following a later winner.
    void api.cancelComposerOperation(sessionId, target.operationId).catch(() => undefined);
  }
  return allObserved;
}
export async function submitAndObserveComposerOperation(descriptor: SubmittedCustody, options: ObserverOptions = {}): Promise<MessageWithStateResponse> {
  const admitted = acquireComposerOperationCustody(descriptor);
  return observeComposerOperation(admitted, { ...options, submit: true });
}
export async function observeComposerOperation(initial: OperationCustody, options: ObserverOptions & { submit?: boolean } = {}): Promise<MessageWithStateResponse> {
  const generation = currentAuthGeneration();
  const owner: ObserverOwner = { generation, descriptor: initial, controller: new AbortController(), stop: composerStopRequested(initial), stopRequested: composerStopRequested(initial), stopTarget: composerStopRequested(initial) ? initial : null };
  const key = ownerKey(initial);
  const previous = owners.get(key);
  if (previous !== undefined) previous.controller.abort();
  owners.set(key, owner);
  const current = () => {
    const scope = composerCustodyScope();
    return !owner.controller.signal.aborted && isCurrentAuthGeneration(generation) && scope !== null && samePrincipal(scope, initial.scope) && (options.current?.() ?? true);
  };
  const check = () => { if (!current()) throw new ComposerObservationDetached(); };
  const abort = () => {
    if (!current()) return;
    if (options.signal?.reason === COMPOSE_USER_CANCEL_ABORT_REASON) { owner.stop = true; owner.stopRequested = true; owner.stopTarget = owner.descriptor; requestComposerStop(owner.descriptor); }
    else owner.controller.abort();
  };
  options.signal?.addEventListener("abort", abort);
  // Restored submitted custody may be an admission whose response was lost.
  // Observation alone never grants another POST or lets another terminal
  // retire the original immutable body.
  let ambiguous = initial.mode === "submitted" && options.submit !== true;
  let shouldSubmit = options.submit === true;
  let streamAttempted = false;
  let backoff = 1000;
  let observationDeadline = Number.POSITIVE_INFINITY;
  let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
  const tightenDeadline = (remainingMs: number) => {
    const candidate = performance.now() + remainingMs + COMPOSE_CLIENT_GRACE_MS;
    if (candidate >= observationDeadline) return;
    observationDeadline = candidate;
    if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
    deadlineTimer = setTimeout(() => {
      try { if (current()) options.timeout?.(); }
      finally { owner.controller.abort(); }
    }, Math.max(0, observationDeadline - performance.now()));
  };
  try {
    if (options.signal?.aborted) {
      // Stop before POST never dispatches an action. Deadlines only detach.
      settleComposerCustody(initial); throw new ComposerObservationDetached();
    }
    while (true) {
      check();
      if (shouldSubmit && replayAgeEligible(owner.descriptor) && !owner.stopRequested && options.submit === true && owner.descriptor.mode === "submitted") {
        shouldSubmit = false;
        try {
          const ack = await api.submitComposerOperation(owner.descriptor);
          check();
          if (ack.operation_id !== owner.descriptor.operationId || ack.kind !== owner.descriptor.kind) throw new Error("Composer acknowledgement identity mismatch");
          ambiguous = false;
        } catch (error) {
          check();
          const active = api.composerActiveOperation(error);
          if (active !== null) {
            if (!ambiguous) { settleComposerCustody(owner.descriptor); if (owner.stopTarget?.operationId === owner.descriptor.operationId) owner.stop = false; }
            owner.descriptor = attachComposerObserver(owner.descriptor, active.operation_id, active.kind, ambiguous);
            // A refused body is never relabelled with the winner's identity.
            // The store keeps the optimistic row as the losing user's draft.
          } else if (api.isDefinitiveComposerRefusal(error)) {
            settleComposerCustody(owner.descriptor); throw error;
          } else {
            ambiguous = true;
          }
        }
      }
      check();
      if (owner.stop) {
        try {
          const cancelled = await api.cancelComposerOperation(initial.sessionId, owner.stopTarget!.operationId);
          check();
          // A missing read cannot prove the earlier admission thread stopped.
          // Keep retrying cancellation if that thread commits later.
          if (cancelled.kind === "session_missing") { clearComposerSessionCustody(initial.scope, initial.sessionId); throw new ComposerSessionMissing(); }
          if (cancelled.kind !== "operation_missing" && cancelled.kind !== owner.stopTarget!.kind) throw new Error("Cancellation kind mismatch");
          owner.stop = cancelled.kind === "operation_missing" || ((cancelled.status === "queued" || cancelled.status === "running") && !cancelled.cancel_requested);
        } catch (error) { check(); if (error instanceof ComposerSessionMissing) throw error; }
      }
      if (!streamAttempted && !ambiguous) {
        streamAttempted = true;
        await readStream(owner, current, tightenDeadline, options.progress);
        check();
      }
      let durableTerminal = false;
      try {
        const snapshot = await api.fetchComposerOperation(initial.sessionId, owner.descriptor.operationId);
        check();
        if (snapshot.kind === "session_missing") {
          clearComposerSessionCustody(initial.scope, initial.sessionId); throw new ComposerSessionMissing();
        }
        if (snapshot.kind === "operation_missing") {
          // This read cannot establish that an older admission thread stopped.
          // Replay remains restricted to the exact submitted descriptor.
          if (ambiguous && replayAgeEligible(owner.descriptor) && owner.descriptor.mode === "submitted" && options.submit === true && !owner.stopRequested && options.reconciliationOnly !== true) shouldSubmit = true;
          await sleep(backoff, owner.controller.signal); backoff = Math.min(backoff * 2, 8000); continue;
        }
        if (snapshot.kind !== owner.descriptor.kind) throw new Error("Operation kind mismatch");
        // A bound authoritative snapshot establishes admission permanently
        // for this observation, even if a later read is missing.
        ambiguous = false;
        backoff = 1000;
        if (snapshot.status === "completed" || snapshot.status === "failed") {
          if (snapshot.status === "failed") validateComposerRecoveryBody(snapshot.error!.body, initial.sessionId);
          const terminalError = snapshot.status === "failed" ? await api.apiErrorFromBody(snapshot.error!.http_status, snapshot.error!.body) : null;
          check();
          durableTerminal = true;
          settleComposerCustody(owner.descriptor);
          if (owner.descriptor.mode === "observer" && initial.mode === "submitted") throw new ComposerActiveAttachment();
          if (snapshot.status === "completed") return snapshot.result!;
          throw terminalError;
        }
        tightenDeadline(snapshot.deadline_remaining_ms);
        await sleep(Math.max(100, Math.min(snapshot.poll_after_ms, 60000)), owner.controller.signal);
      } catch (error) {
        check();
        if (durableTerminal || error instanceof ComposerSessionMissing || error instanceof ComposerActiveAttachment || api.isDefinitiveComposerRefusal(error)) throw error;
        // Decode/transport/GET outages preserve pending custody and retry under
        // the same generation. They do not fail or replay an observed action.
        await sleep(backoff, owner.controller.signal); backoff = Math.min(backoff * 2, 8000);
      }
    }
  } finally {
    if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
    options.signal?.removeEventListener("abort", abort);
    if (owners.get(key) === owner) owners.delete(key);
  }
}
async function readStream(owner: ObserverOwner, current: () => boolean, tightenDeadline: (remainingMs: number) => void, progress?: (snapshot: ComposerProgressSnapshot) => void): Promise<void> {
  const controller = new AbortController();
  const close = () => controller.abort();
  owner.controller.signal.addEventListener("abort", close, { once: true });
  let timer = setTimeout(close, 15000);
  const lifetimeTimer = setTimeout(close, 120000);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    const response = await api.fetchComposerOperationStream(owner.descriptor.sessionId, owner.descriptor.operationId, controller.signal);
    if (!current() || !response.ok || response.headers.get("content-type")?.split(";")[0] !== "text/event-stream" || response.body === null) return;
    reader = response.body.getReader();
    const decoder = new ComposerSseDecoder();
    let sequence = -1;
    while (current()) {
      const chunk = await reader.read();
      if (!current()) return;
      if (chunk.done) { decoder.finish(); return; }
      for (const value of decoder.push(chunk.value)) {
        const frame = decodeComposerOperationFrame(value, owner.descriptor.sessionId, owner.descriptor.operationId);
        if (frame.sequence <= sequence) continue;
        sequence = frame.sequence;
        clearTimeout(timer); timer = setTimeout(close, 15000);
        if (frame.event === "status") tightenDeadline(frame.payload.deadline_remaining_ms);
        if (frame.event === "terminal") return;
        if (frame.event === "progress") progress?.({ ...frame.payload, session_id: frame.session_id, inflight_requests: 1 });
      }
    }
  } catch { /* One authenticated durable GET observer handles every fallback. */ }
  finally {
    clearTimeout(timer); clearTimeout(lifetimeTimer); controller.abort(); owner.controller.signal.removeEventListener("abort", close);
    if (reader !== undefined) { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
  }
}
