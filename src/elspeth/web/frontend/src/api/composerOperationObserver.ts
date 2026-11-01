// Composer operation admission and observation (Continuation design §3.5–§3.7, §4.3).
//
// Every compose submission (freeform and tutorial alike) passes through
// submitAndObserveComposerOperation; every restored or adopted operation
// through observeComposerOperation. One owner per operation identity holds the
// observation from the POST to the sealed terminal.
//
// Resume is state transfer: every status frame and every GET is a complete
// snapshot keyed by the parent's SQL status_revision, so any replica can serve
// any reconnect. The client accepts a strictly greater revision, ignores an
// equal or smaller one, and treats an equal revision with different state as
// a protocol error (it then trusts only the GET). There is no total deadline:
// a segment deadline schedules one GET and never ends observation.
//
// Retirement of custody happens only on a sealed terminal, a definitive
// admission refusal, or the exact missing-session answer. A refused read
// never proves an operation finished.
import * as api from "./client";
import { currentAuthGeneration, isCurrentAuthGeneration } from "./authSession";
import { ComposerObservationDetached, ComposerSessionMissing } from "./composerObserverErrors";
import { ComposerSseDecoder, decodeComposerOperationFrame, validateComposerRecoveryBody } from "./composerOperationDecoder";
import { ComposerObservationFault, deliverComposerObservation } from "./composerObservationFault";
import { COMPOSER_CUSTODY_LIFETIME_MS, acquireComposerOperationCustody, attachComposerObserver, clearComposerSessionCustody, clearComposerStop, composerCustodyScope, composerStopRequested, findComposerOperationCustody, requestComposerStop, samePrincipal, settleComposerCustody } from "@/stores/composerOperationCustody";
import { COMPOSE_USER_CANCEL_ABORT_REASON } from "@/config/composer";
import type { ApiError, ComposerProgressSnapshot, MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationSnapshot, ComposerOperationStatus, OperationCustody, SubmittedCustody } from "@/types/composerOperations";

export { ComposerObservationDetached, ComposerObservationFault, ComposerSessionMissing };
/** The winner's authenticated terminal is separate from the refused submission. */
export class ComposerActiveAttachment extends Error {
  constructor(readonly terminal: { status: "completed"; result: MessageWithStateResponse } | { status: "failed"; error: ApiError }) {
    super(`Another composer action ${terminal.status === "failed" ? "failed" : "finished"}. Your unsent content remains a draft.`);
    this.name = "ComposerActiveAttachment";
  }
}

export const COMPOSER_STREAM_IDLE_CLOSE_MS = 25_000;
export const COMPOSER_STREAM_LIFETIME_MS = 120_000;
const BACKOFF_CAP_MS = 8_000;
const DEFAULT_CAPACITY_RETRY_MS = 5_000;

export interface ComposerObserverOptions {
  signal?: AbortSignal;
  current?: () => boolean;
  progress?: (snapshot: ComposerProgressSnapshot) => void;
  /** Every accepted state, in revision order (phase, segment, Stop). */
  status?: (status: ComposerOperationStatus) => void;
  /** Live-update health: true after two failed connects, false on the next healthy one. */
  interrupted?: (interrupted: boolean) => void;
  /** Stop was asked for but the server has not acknowledged it yet. */
  stopUnconfirmed?: (unconfirmed: boolean) => void;
  /** This build refused Stop; the operation remains under observation. */
  stopUnavailable?: () => void;
}

interface Owner { generation: number; descriptor: OperationCustody; controller: AbortController; wake: () => void }
const owners = new Set<Owner>();
function identity(descriptor: OperationCustody): string {
  return JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]);
}
function live(owner: Owner): boolean { return !owner.controller.signal.aborted && isCurrentAuthGeneration(owner.generation); }
function replayAgeEligible(descriptor: OperationCustody): boolean {
  const age = Date.now() - descriptor.createdAt;
  return age >= 0 && age <= COMPOSER_CUSTODY_LIFETIME_MS;
}
/** A wakeable sleep: Stop and other durable intent end it at once. */
function sleep(owner: Owner, ms: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (owner.controller.signal.aborted) { reject(new ComposerObservationDetached()); return; }
    const done = () => { clearTimeout(timer); owner.wake = () => undefined; owner.controller.signal.removeEventListener("abort", aborted); resolve(); };
    const aborted = () => { clearTimeout(timer); owner.wake = () => undefined; reject(new ComposerObservationDetached()); };
    const timer = setTimeout(done, ms);
    owner.wake = done;
    owner.controller.signal.addEventListener("abort", aborted, { once: true });
  });
}

export function detachComposerObservers(sessionId?: string): void {
  for (const owner of owners) if (sessionId === undefined || owner.descriptor.sessionId === sessionId) owner.controller.abort();
}
/**
 * Record Stop for every held operation of the session and send it. Returns
 * false when some held operation has no live observer in this tab, so the
 * caller can resume one to see the outcome.
 */
export function cancelObservedComposerOperation(sessionId: string): boolean {
  const scope = composerCustodyScope();
  if (scope === null) return false;
  const record = findComposerOperationCustody(scope, sessionId);
  const targets = new Map<string, OperationCustody>(record.unresolvedSubmissions);
  if (record.foreground !== null) targets.set(record.foreground.operationId, record.foreground);
  if (targets.size === 0) return false;
  let allObserved = true;
  for (const target of targets.values()) {
    requestComposerStop(target);
    const observers = [...owners].filter((owner) => live(owner) && owner.descriptor.operationId === target.operationId && owner.descriptor.sessionId === sessionId && samePrincipal(owner.descriptor.scope, scope));
    if (observers.length === 0) allObserved = false;
    // The observer re-sends Stop until the server acknowledges it.
    for (const owner of observers) owner.wake();
    // Only durable terminal establishes settlement. Capture each held target,
    // including unresolved admissions, without following a later winner.
    void api.cancelComposerOperation(sessionId, target.operationId).catch(() => undefined);
  }
  return allObserved;
}

export async function submitAndObserveComposerOperation(descriptor: SubmittedCustody, options: ComposerObserverOptions = {}): Promise<MessageWithStateResponse> {
  return run(acquireComposerOperationCustody(descriptor), options, true);
}
/** Observe held custody. Observation alone never grants another POST. */
export async function observeComposerOperation(descriptor: OperationCustody, options: ComposerObserverOptions = {}): Promise<MessageWithStateResponse> {
  return run(descriptor, options, false);
}

async function run(initial: OperationCustody, options: ComposerObserverOptions, submit: boolean): Promise<MessageWithStateResponse> {
  for (const other of owners) if (identity(other.descriptor) === identity(initial)) other.controller.abort();
  const owner: Owner = { generation: currentAuthGeneration(), descriptor: initial, controller: new AbortController(), wake: () => undefined };
  owners.add(owner);
  const current = () => {
    const scope = composerCustodyScope();
    return live(owner) && scope !== null && samePrincipal(scope, initial.scope) && (options.current?.() ?? true);
  };
  const check = () => { if (!current()) throw new ComposerObservationDetached(); };
  // A user Stop delivered through the request signal is durable Stop intent;
  // any other abort (a deadline, navigation) only detaches this observer.
  const onAbort = () => {
    if (!current()) return;
    if (options.signal?.reason === COMPOSE_USER_CANCEL_ABORT_REASON) { requestComposerStop(owner.descriptor); owner.wake(); }
    else owner.controller.abort();
  };
  options.signal?.addEventListener("abort", onAbort);
  let first: ComposerOperationSnapshot | null = null;
  try {
    if (submit) {
      // Stop before POST never dispatches an action.
      if (options.signal?.aborted) { settleComposerCustody(initial); throw new ComposerObservationDetached(); }
      first = await admit(owner, initial as SubmittedCustody, check);
    }
    if (!(owner.descriptor.mode === "observer" && initial.mode === "submitted")) return await follow(owner, options, current, check, first);
    // A refused submission observes the winner, whose terminal keeps the
    // "another action finished" contract; the refused body stays a draft.
    let result: MessageWithStateResponse;
    try { result = await follow(owner, options, current, check, null); }
    catch (error) {
      if (typeof error === "object" && error !== null && "composer_operation" in error) throw new ComposerActiveAttachment({ status: "failed", error: error as ApiError });
      throw error;
    }
    throw new ComposerActiveAttachment({ status: "completed", result });
  } finally {
    options.signal?.removeEventListener("abort", onAbort);
    owners.delete(owner);
  }
}

/**
 * POST the submitted body. Returns once the admission is established (or the
 * owner now observes the active winner). An ambiguous admission is resolved
 * only by a read of the same identity, whose snapshot is returned for the
 * observer to apply; a missing read may replay the same immutable ID and body
 * while the custody is young and no Stop was asked for.
 */
async function admit(owner: Owner, descriptor: SubmittedCustody, check: () => void): Promise<ComposerOperationSnapshot | null> {
  let ambiguous = false;
  let shouldSubmit = true;
  let backoff = 1000;
  while (true) {
    check();
    if (shouldSubmit && replayAgeEligible(descriptor) && !composerStopRequested(descriptor)) {
      shouldSubmit = false;
      try {
        const admitted = await api.submitComposerOperation(descriptor);
        check();
        if (admitted.operation_id !== descriptor.operationId || admitted.kind !== descriptor.kind) throw new ComposerObservationFault("ack_identity_mismatch", new Error("Composer acknowledgement identity mismatch"));
        return null;
      } catch (error) {
        check();
        if (error instanceof ComposerObservationFault) throw error;
        const active = api.composerActiveOperation(error);
        if (active !== null) {
          if (!ambiguous) settleComposerCustody(descriptor);
          // A refused body is never relabelled with the winner's identity.
          // The store keeps the optimistic row as the losing user's draft.
          owner.descriptor = attachComposerObserver(descriptor, active.operation_id, active.kind, ambiguous);
          return null;
        }
        if (api.isDefinitiveComposerRefusal(error)) { settleComposerCustody(descriptor); throw error; }
        ambiguous = true;
      }
    }
    // Not ambiguous (nothing was dispatched) or Stop was asked for: observe it.
    if (!ambiguous || composerStopRequested(descriptor)) return null;
    try {
      const read = await api.fetchComposerOperation(descriptor.sessionId, descriptor.operationId);
      check();
      if ("operation_id" in read) {
        if (read.kind !== descriptor.kind) throw new ComposerObservationFault("snapshot_kind_mismatch", new Error("Operation kind mismatch"));
        // A bound authoritative snapshot establishes admission permanently.
        return read;
      }
      if (read.kind === "session_missing") { clearComposerSessionCustody(descriptor.scope, descriptor.sessionId); throw new ComposerSessionMissing(); }
      // This read cannot establish that an older admission thread stopped.
      // Replay remains restricted to the exact submitted descriptor.
      shouldSubmit = true;
    } catch (error) {
      check();
      if (error instanceof ComposerObservationFault || error instanceof ComposerSessionMissing || api.isDefinitiveComposerRefusal(error)) throw error;
      // Decode/transport/GET outages keep custody and retry the same read.
    }
    await sleep(owner, backoff); backoff = Math.min(backoff * 2, BACKOFF_CAP_MS);
  }
}

/** Revision bookkeeping, exported for its own tests. */
export class RevisionGate {
  private highest = -1;
  private accepted: ComposerOperationStatus | null = null;
  get lastAccepted(): number { return this.highest; }
  get latest(): ComposerOperationStatus | null { return this.accepted; }
  /** True for a new state; false for a duplicate or stale one; throws on an equal-revision conflict. */
  accept(status: ComposerOperationStatus): boolean {
    if (status.status_revision < this.highest) return false;
    if (status.status_revision === this.highest && this.accepted !== null) {
      const same = status.status === this.accepted.status && status.phase === this.accepted.phase && status.cancel_requested === this.accepted.cancel_requested
        && JSON.stringify(status.segment) === JSON.stringify(this.accepted.segment);
      if (!same) throw new Error("Composer protocol error: equal revision with different state");
      return false;
    }
    this.highest = status.status_revision; this.accepted = status;
    return true;
  }
}

type StreamEnd = "terminal" | "reconnect" | "deadline" | "capacity" | "failed";
type StatusAcceptance = "fresh" | "duplicate" | "stale" | "conflict";

/**
 * Observe an admitted (or restored, or adopted) operation to its sealed
 * terminal. `first` is a snapshot already read for this identity (the read
 * that resolved an ambiguous admission); it is applied before anything else.
 */
async function follow(owner: Owner, options: ComposerObserverOptions, current: () => boolean, check: () => void, first: ComposerOperationSnapshot | null): Promise<MessageWithStateResponse> {
  const descriptor = owner.descriptor;
  const controller = owner.controller;
  const gate = new RevisionGate();
  let trustStream = true;
  let consecutiveFailures = 0;
  let stopFailures = 0;
  let stopAcknowledged = false;
  let capacityUntil = 0;
  const consumedDeadlines = new Set<string>();
  let acceptedDeadline: { key: string; dueAt: number } | null = null;
  const pollAfter = () => Math.max(100, Math.min(gate.latest?.poll_after_ms ?? 1000, 60_000));
  const backoff = (failures: number) => Math.min(1000 * 2 ** Math.max(0, failures - 1), BACKOFF_CAP_MS);

  const accept = (status: ComposerOperationStatus, fault: "snapshot_kind_mismatch" | "cancel_kind_mismatch" = "snapshot_kind_mismatch"): StatusAcceptance => {
    if (status.kind !== descriptor.kind) throw new ComposerObservationFault(fault, new Error("Operation kind mismatch"));
    let fresh: boolean;
    try { fresh = gate.accept(status); } catch { trustStream = false; return "conflict"; }
    if (!fresh) return status.status_revision < gate.lastAccepted ? "stale" : "duplicate";
    const segment = status.segment;
    if (segment === null || segment.deadline_at === null) acceptedDeadline = null;
    else {
      const key = `${segment.sequence}:${segment.generation}:${segment.deadline_at}`;
      if (acceptedDeadline?.key !== key) {
        acceptedDeadline = { key, dueAt: performance.now() + Math.max(0, Date.parse(segment.deadline_at) - Date.parse(status.observed_at)) };
      }
    }
    deliverComposerObservation(() => options.status?.(status));
    if (status.cancel_requested) {
      stopAcknowledged = true;
      deliverComposerObservation(() => options.stopUnconfirmed?.(false));
    }
    return "fresh";
  };

  // One connection. A connection is healthy only when it ends with terminal or
  // reconnect; frames received before an unexplained end do not reset failures.
  const readStream = async (): Promise<StreamEnd | { capacityMs: number }> => {
    const stream = new AbortController();
    const close = () => stream.abort();
    controller.signal.addEventListener("abort", close, { once: true });
    let idle = setTimeout(close, COMPOSER_STREAM_IDLE_CLOSE_MS);
    const lifetime = setTimeout(close, COMPOSER_STREAM_LIFETIME_MS + 5_000);
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
    let deadlineFired = false;
    const armAcceptedDeadline = () => {
      if (deadlineTimer !== undefined) { clearTimeout(deadlineTimer); deadlineTimer = undefined; }
      const deadline = acceptedDeadline;
      if (deadline === null || consumedDeadlines.has(deadline.key)) return;
      deadlineTimer = setTimeout(() => { consumedDeadlines.add(deadline.key); deadlineFired = true; close(); }, Math.max(0, deadline.dueAt - performance.now()));
    };
    const previousWake = owner.wake;
    owner.wake = close;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      // A reconnect retains the original accepted deadline, even if its first
      // snapshot repeats that state with a different observation timestamp.
      armAcceptedDeadline();
      const response = await api.fetchComposerOperationStream(descriptor.sessionId, descriptor.operationId, stream.signal, gate.lastAccepted >= 0 ? gate.lastAccepted : null);
      if (!current()) return "failed";
      if (response.status === 503) {
        const seconds = Number(response.headers.get("Retry-After"));
        return { capacityMs: Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, 60_000) : DEFAULT_CAPACITY_RETRY_MS };
      }
      if (!response.ok || response.headers.get("content-type")?.split(";")[0] !== "text/event-stream" || response.body === null) return "failed";
      reader = response.body.getReader();
      const decoder = new ComposerSseDecoder();
      let sequence = -1;
      while (current()) {
        const chunk = await reader.read();
        if (!current()) return "failed";
        if (chunk.done) { decoder.finish(); break; }
        for (const raw of decoder.push(chunk.value)) {
          const frame = decodeComposerOperationFrame(raw, descriptor.sessionId, descriptor.operationId);
          if (frame.sequence <= sequence) continue;
          sequence = frame.sequence;
          clearTimeout(idle); idle = setTimeout(close, COMPOSER_STREAM_IDLE_CLOSE_MS);
          if (frame.event === "status") {
            const decision = accept(frame.payload);
            if (!trustStream) return "failed";
            if (decision !== "fresh") continue;
            armAcceptedDeadline();
            if (frame.payload.status === "completed" || frame.payload.status === "failed") return "terminal";
          } else if (frame.event === "progress") {
            deliverComposerObservation(() => options.progress?.({ ...frame.payload, session_id: descriptor.sessionId, inflight_requests: 1 } as ComposerProgressSnapshot));
          } else if (frame.event === "terminal") {
            if (frame.payload.status_revision < gate.lastAccepted) continue;
            if (frame.payload.status_revision === gate.lastAccepted) {
              const accepted = gate.latest!;
              if (frame.payload.status !== accepted.status || frame.payload.phase !== accepted.phase) { trustStream = false; return "failed"; }
            }
            return "terminal";
          } else if (frame.event === "reconnect") return "reconnect";
        }
      }
      return deadlineFired ? "deadline" : "failed";
    } catch (error) {
      if (error instanceof ComposerObservationFault) throw error;
      return deadlineFired ? "deadline" : "failed";
    } finally {
      clearTimeout(idle); clearTimeout(lifetime); if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
      controller.signal.removeEventListener("abort", close);
      owner.wake = previousWake;
      stream.abort();
      if (reader !== undefined) { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
    }
  };

  // The durable GET. Returns the sealed result, throws a sealed failure, or
  // returns "live" / "missing".
  type Durable = { done: true; result: MessageWithStateResponse } | { done: false; why: "live" | "missing" };
  const readDurable = async (): Promise<Durable> => {
    const read = await api.fetchComposerOperation(descriptor.sessionId, descriptor.operationId);
    check();
    if (!("operation_id" in read)) {
      if (read.kind === "session_missing") { clearComposerSessionCustody(descriptor.scope, descriptor.sessionId); throw new ComposerSessionMissing(); }
      return { done: false, why: "missing" };
    }
    return apply(read);
  };
  const apply = async (snapshot: ComposerOperationSnapshot): Promise<Durable> => {
    const decision = accept(snapshot);
    if (decision === "stale" || decision === "conflict") return { done: false, why: "live" };
    if (snapshot.status !== "completed" && snapshot.status !== "failed") return { done: false, why: "live" };
    // The terminal GET is read for its sealed result even at an equal revision.
    if (snapshot.status === "failed") validateComposerRecoveryBody(snapshot.error!.body, descriptor.sessionId);
    const bodyError = snapshot.status === "failed" ? await api.apiErrorFromBody(snapshot.error!.http_status, snapshot.error!.body) : null;
    check();
    settleComposerCustody(descriptor);
    if (snapshot.status === "completed") return { done: true, result: snapshot.result! };
    throw {
      ...bodyError!,
      failure_code: snapshot.error!.failure_code === "http_error" ? bodyError!.failure_code : snapshot.error!.failure_code,
      diagnostic_id: snapshot.error!.diagnostic_id ?? undefined,
      composer_operation: { operation_id: snapshot.operation_id, kind: snapshot.kind, failure_code: snapshot.error!.failure_code },
    };
  };
  // A sealed failure or a lost session ends observation; any other read error keeps custody and retries.
  const isFinal = (error: unknown) => error instanceof ComposerSessionMissing || error instanceof ComposerObservationDetached || (typeof error === "object" && error !== null && "composer_operation" in error);

  if (first !== null) {
    try {
      const durable = await apply(first);
      if (durable.done) return durable.result;
    } catch (error) {
      check();
      if (error instanceof ComposerObservationFault || isFinal(error)) throw error;
      // A malformed recovery body keeps custody; the loop re-reads the GET.
    }
  }
  while (true) {
    check();
    // Unconfirmed Stop has its own retry schedule and is not held behind a stream.
    if (composerStopRequested(descriptor) && !stopAcknowledged) {
      try {
        const stopped = await api.cancelComposerOperation(descriptor.sessionId, descriptor.operationId);
        check();
        if (!("operation_id" in stopped)) {
          if (stopped.kind === "session_missing") { clearComposerSessionCustody(descriptor.scope, descriptor.sessionId); throw new ComposerSessionMissing(); }
          stopFailures += 1;
        } else {
          const decision = accept(stopped, "cancel_kind_mismatch");
          if (decision === "fresh" || decision === "duplicate") {
            const accepted = gate.latest!;
            stopAcknowledged = accepted.cancel_requested || accepted.status === "completed" || accepted.status === "failed";
          }
          stopFailures = stopAcknowledged ? 0 : stopFailures + 1;
        }
      } catch (error) {
        check();
        if (error instanceof ComposerObservationFault || error instanceof ComposerSessionMissing) throw error;
        if (typeof error === "object" && error !== null && "status" in error && error.status === 409
          && "error_type" in error && error.error_type === "composer_continuation_stop_unavailable") {
          clearComposerStop(descriptor);
          stopFailures = 0;
          deliverComposerObservation(() => options.stopUnconfirmed?.(false));
          deliverComposerObservation(() => options.stopUnavailable?.());
        } else stopFailures += 1;
      }
      if (!stopAcknowledged && composerStopRequested(descriptor)) {
        deliverComposerObservation(() => options.stopUnconfirmed?.(true));
        await sleep(owner, backoff(stopFailures));
        continue;
      }
      deliverComposerObservation(() => options.stopUnconfirmed?.(false));
    }
    let end: StreamEnd = "failed";
    if (Date.now() < capacityUntil || !trustStream) end = "deadline"; // poll the GET this round
    else {
      const outcome = await readStream();
      check();
      if (typeof outcome === "object") { capacityUntil = Date.now() + outcome.capacityMs; end = "deadline"; }
      else end = outcome;
    }
    if (end === "reconnect") { consecutiveFailures = 0; deliverComposerObservation(() => options.interrupted?.(false)); continue; }
    if (end === "failed") {
      consecutiveFailures += 1;
      if (consecutiveFailures >= 2) deliverComposerObservation(() => options.interrupted?.(true));
    } else if (end === "terminal") { consecutiveFailures = 0; deliverComposerObservation(() => options.interrupted?.(false)); }
    // Terminal, deadline, capacity, an untrusted stream, or a repeated failure: read the durable GET.
    if (end !== "failed" || consecutiveFailures >= 2) {
      try {
        const durable = await readDurable();
        if (durable.done) return durable.result;
      } catch (error) {
        check();
        if (error instanceof ComposerObservationFault) throw error;
        if (isFinal(error)) throw error;
        if (api.isDefinitiveComposerRefusal(error)) throw error; // custody is kept: a refused read proves nothing
      }
    }
    if (end === "failed") await sleep(owner, backoff(consecutiveFailures));
    else if (end === "terminal") await sleep(owner, backoff(1)); // the stream said terminal but the GET did not yet
    else if (end === "deadline") await sleep(owner, Date.now() < capacityUntil || !trustStream ? pollAfter() : 0);
  }
}
