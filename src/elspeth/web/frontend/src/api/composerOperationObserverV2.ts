// Composer-Operation-Version 2 observation (design §3.5–§3.7, §4.3).
//
// Resume is state transfer: every status frame and every GET is a complete
// snapshot keyed by the parent's SQL status_revision, so any replica can serve
// any reconnect. The client accepts a strictly greater revision, ignores an
// equal or smaller one, and treats an equal revision with different state as
// a protocol error (it then trusts only the GET). There is no total deadline:
// a segment deadline schedules one GET and never ends observation.
//
// Retirement of custody happens only on a sealed terminal or the exact
// missing-session answer. A refused read never proves an operation finished.
import * as api from "./client";
import { currentAuthGeneration, isCurrentAuthGeneration } from "./authSession";
import { ComposerObservationDetached, ComposerSessionMissing } from "./composerObserverErrors";
import { ComposerSseDecoderV2, decodeComposerOperationFrameV2 } from "./composerOperationDecoderV2";
import { validateComposerRecoveryBody } from "./composerOperationDecoder";
import { clearComposerSessionCustody, clearComposerStop, composerCustodyScope, composerStopRequested, recordComposerProtocolVersion, samePrincipal, settleComposerCustody } from "@/stores/composerOperationCustody";
import type { ComposerProgressSnapshot, MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationStatusV2, OperationCustody } from "@/types/composerOperations";

export const V2_IDLE_CLOSE_MS = 25_000;
export const V2_STREAM_LIFETIME_MS = 120_000;
const BACKOFF_CAP_MS = 8_000;
const DEFAULT_CAPACITY_RETRY_MS = 5_000;

export interface ObserverV2Options {
  signal?: AbortSignal;
  current?: () => boolean;
  progress?: (snapshot: ComposerProgressSnapshot) => void;
  /** Every accepted v2 state, in revision order (phase, segment, Stop). */
  status?: (status: ComposerOperationStatusV2) => void;
  /** Live-update health: true after two failed connects, false on the next healthy one. */
  interrupted?: (interrupted: boolean) => void;
  /** Stop was asked for but the server has not acknowledged it yet. */
  stopUnconfirmed?: (unconfirmed: boolean) => void;
  /** This build refused Stop; the operation remains under observation. */
  stopUnavailable?: () => void;
  /** The server answered this operation as lifecycle 1: observe it with the v1 observer. */
  handBackToV1?: (descriptor: OperationCustody) => Promise<MessageWithStateResponse>;
}

type Owner = { controller: AbortController; wake: () => void };
const owners = new Map<string, Owner>();
function ownerKey(descriptor: OperationCustody): string {
  return JSON.stringify([descriptor.scope.principalId, descriptor.scope.authProvider, descriptor.sessionId, descriptor.operationId]);
}
/** Stop and other durable intent wake a sleeping or streaming v2 observer at once. */
export function wakeComposerObserverV2(sessionId: string, operationId: string): void {
  for (const [key, owner] of owners) {
    const parsed = JSON.parse(key) as string[];
    if (parsed[2] === sessionId && parsed[3] === operationId) owner.wake();
  }
}
export function detachComposerObserversV2(sessionId?: string): void {
  for (const [key, owner] of owners) if (sessionId === undefined || (JSON.parse(key) as string[])[2] === sessionId) owner.controller.abort();
}

/** Revision bookkeeping, exported for its own tests. */
export class RevisionGate {
  private highest = -1;
  private accepted: ComposerOperationStatusV2 | null = null;
  get lastAccepted(): number { return this.highest; }
  get latest(): ComposerOperationStatusV2 | null { return this.accepted; }
  /** True for a new state; false for a duplicate or stale one; throws on an equal-revision conflict. */
  accept(status: ComposerOperationStatusV2): boolean {
    if (status.status_revision < this.highest) return false;
    if (status.status_revision === this.highest && this.accepted !== null) {
      const same = status.status === this.accepted.status && status.phase === this.accepted.phase && status.cancel_requested === this.accepted.cancel_requested
        && JSON.stringify(status.segment) === JSON.stringify(this.accepted.segment);
      if (!same) throw new Error("Composer v2 protocol error: equal revision with different state");
      return false;
    }
    this.highest = status.status_revision; this.accepted = status;
    return true;
  }
}

type StreamEnd = "terminal" | "reconnect" | "deadline" | "capacity" | "failed";

export async function observeComposerOperationV2(descriptor: OperationCustody, options: ObserverV2Options = {}): Promise<MessageWithStateResponse> {
  const generation = currentAuthGeneration();
  const key = ownerKey(descriptor);
  owners.get(key)?.controller.abort();
  const controller = new AbortController();
  let wake: () => void = () => undefined;
  const owner: Owner = { controller, wake: () => wake() };
  owners.set(key, owner);
  const current = () => {
    const scope = composerCustodyScope();
    return !controller.signal.aborted && !(options.signal?.aborted ?? false) && isCurrentAuthGeneration(generation) && scope !== null && samePrincipal(scope, descriptor.scope) && (options.current?.() ?? true);
  };
  const check = () => { if (!current()) throw new ComposerObservationDetached(); };
  const onAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  const gate = new RevisionGate();
  let trustStream = true;
  let consecutiveFailures = 0;
  let stopFailures = 0;
  let stopAcknowledged = false;
  let capacityUntil = 0;
  const consumedDeadlines = new Set<string>();
  const pollAfter = () => Math.max(100, Math.min(gate.latest?.poll_after_ms ?? 1000, 60_000));
  const backoff = (failures: number) => Math.min(1000 * 2 ** Math.max(0, failures - 1), BACKOFF_CAP_MS);

  const sleep = (ms: number) => new Promise<void>((resolve, reject) => {
    if (!current()) { reject(new ComposerObservationDetached()); return; }
    const done = () => { clearTimeout(timer); wake = () => undefined; controller.signal.removeEventListener("abort", aborted); resolve(); };
    const aborted = () => { clearTimeout(timer); wake = () => undefined; reject(new ComposerObservationDetached()); };
    const timer = setTimeout(done, ms);
    wake = done;
    controller.signal.addEventListener("abort", aborted, { once: true });
  });
  const accept = (status: ComposerOperationStatusV2): boolean => {
    let fresh: boolean;
    try { fresh = gate.accept(status); } catch { trustStream = false; return false; }
    if (fresh) {
      options.status?.(status);
      if (status.cancel_requested) { stopAcknowledged = true; options.stopUnconfirmed?.(false); }
    }
    return fresh;
  };

  // One connection. A connection is healthy only when it ends with terminal or
  // reconnect; frames received before an unexplained end do not reset failures.
  const readStream = async (): Promise<StreamEnd | { capacityMs: number }> => {
    const stream = new AbortController();
    const close = () => stream.abort();
    controller.signal.addEventListener("abort", close, { once: true });
    let idle = setTimeout(close, V2_IDLE_CLOSE_MS);
    const lifetime = setTimeout(close, V2_STREAM_LIFETIME_MS + 5_000);
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
    let deadlineFired = false;
    const previousWake = wake;
    wake = close;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await api.fetchComposerOperationStreamV2(descriptor.sessionId, descriptor.operationId, stream.signal, gate.lastAccepted >= 0 ? gate.lastAccepted : null);
      if (!current()) return "failed";
      if (response.status === 503) {
        const seconds = Number(response.headers.get("Retry-After"));
        return { capacityMs: Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, 60_000) : DEFAULT_CAPACITY_RETRY_MS };
      }
      if (!response.ok || response.headers.get("content-type")?.split(";")[0] !== "text/event-stream" || response.body === null) return "failed";
      reader = response.body.getReader();
      const decoder = new ComposerSseDecoderV2();
      let sequence = -1;
      while (current()) {
        const chunk = await reader.read();
        if (!current()) return "failed";
        if (chunk.done) { decoder.finish(); break; }
        for (const raw of decoder.push(chunk.value)) {
          const frame = decodeComposerOperationFrameV2(raw, descriptor.sessionId, descriptor.operationId);
          if (frame.sequence <= sequence) continue;
          sequence = frame.sequence;
          clearTimeout(idle); idle = setTimeout(close, V2_IDLE_CLOSE_MS);
          if (frame.event === "status") {
            accept(frame.payload);
            if (!trustStream) return "failed";
            if (frame.payload.status === "completed" || frame.payload.status === "failed") return "terminal";
            const segment = frame.payload.segment;
            const deadlineKey = segment?.deadline_at == null ? null : `${segment.sequence}:${segment.generation}:${segment.deadline_at}`;
            if (deadlineKey !== null && !consumedDeadlines.has(deadlineKey)) {
              if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
              const delay = Math.max(0, Date.parse(segment!.deadline_at!) - Date.parse(frame.payload.observed_at));
              deadlineTimer = setTimeout(() => { consumedDeadlines.add(deadlineKey); deadlineFired = true; close(); }, delay);
            }
          } else if (frame.event === "progress") {
            options.progress?.({ ...frame.payload, session_id: descriptor.sessionId, inflight_requests: 1 } as ComposerProgressSnapshot);
          } else if (frame.event === "terminal") return "terminal";
          else if (frame.event === "reconnect") return "reconnect";
        }
      }
      return deadlineFired ? "deadline" : "failed";
    } catch {
      return deadlineFired ? "deadline" : "failed";
    } finally {
      clearTimeout(idle); clearTimeout(lifetime); if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
      controller.signal.removeEventListener("abort", close);
      wake = previousWake;
      stream.abort();
      if (reader !== undefined) { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
    }
  };

  // The durable GET. Returns the sealed result, throws a sealed failure, or
  // returns "live" / "missing" / "v1".
  type Durable = { done: true; result: MessageWithStateResponse } | { done: false; why: "live" | "missing" | "v1" };
  const readDurable = async (): Promise<Durable> => {
    const read = await api.fetchComposerOperationAnyVersion(descriptor.sessionId, descriptor.operationId);
    check();
    if (!("protocolVersion" in read)) {
      if (read.kind === "session_missing") { clearComposerSessionCustody(descriptor.scope, descriptor.sessionId); throw new ComposerSessionMissing(); }
      return { done: false, why: "missing" };
    }
    if (read.protocolVersion === 1) return { done: false, why: "v1" };
    const snapshot = read.snapshot;
    accept(snapshot);
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

  try {
    while (true) {
      check();
      // Unconfirmed Stop has its own retry schedule and is not held behind a stream.
      if (composerStopRequested(descriptor) && !stopAcknowledged) {
        try {
          const stopped = await api.cancelComposerOperationV2(descriptor.sessionId, descriptor.operationId);
          check();
          if (!("protocol_version" in stopped)) {
            if (stopped.kind === "session_missing") { clearComposerSessionCustody(descriptor.scope, descriptor.sessionId); throw new ComposerSessionMissing(); }
            stopFailures += 1;
          } else {
            accept(stopped);
            stopAcknowledged = stopped.cancel_requested || stopped.status === "completed" || stopped.status === "failed";
            stopFailures = stopAcknowledged ? 0 : stopFailures + 1;
          }
        } catch (error) {
          check();
          if (error instanceof ComposerSessionMissing) throw error;
          if (typeof error === "object" && error !== null && "status" in error && error.status === 409
            && "error_type" in error && error.error_type === "composer_continuation_stop_unavailable") {
            clearComposerStop(descriptor);
            stopFailures = 0;
            options.stopUnconfirmed?.(false);
            options.stopUnavailable?.();
          } else stopFailures += 1;
        }
        if (!stopAcknowledged && composerStopRequested(descriptor)) {
          options.stopUnconfirmed?.(true);
          await sleep(backoff(stopFailures));
          continue;
        }
        options.stopUnconfirmed?.(false);
      }
      let end: StreamEnd = "failed";
      if (Date.now() < capacityUntil || !trustStream) end = "deadline"; // poll the GET this round
      else {
        const outcome = await readStream();
        check();
        if (typeof outcome === "object") { capacityUntil = Date.now() + outcome.capacityMs; end = "deadline"; }
        else end = outcome;
      }
      if (end === "reconnect") { consecutiveFailures = 0; options.interrupted?.(false); continue; }
      if (end === "failed") {
        consecutiveFailures += 1;
        if (consecutiveFailures >= 2) options.interrupted?.(true);
      } else if (end === "terminal") { consecutiveFailures = 0; options.interrupted?.(false); }
      // Terminal, deadline, capacity, an untrusted stream, or a repeated failure: read the durable GET.
      if (end !== "failed" || consecutiveFailures >= 2) {
        try {
          const durable = await readDurable();
          if (durable.done) return durable.result;
          if (durable.why === "v1") {
            const v1 = recordComposerProtocolVersion(descriptor, 1);
            if (options.handBackToV1 === undefined) throw new Error("A lifecycle-1 operation has no v1 observer to hand back to");
            return await options.handBackToV1(v1);
          }
        } catch (error) {
          check();
          if (isFinal(error)) throw error;
          if (api.isDefinitiveComposerRefusal(error)) throw error; // custody is kept: a refused read proves nothing
        }
      }
      if (end === "failed") await sleep(backoff(consecutiveFailures));
      else if (end === "terminal") await sleep(backoff(1)); // the stream said terminal but the GET did not yet
      else if (end === "deadline") await sleep(Date.now() < capacityUntil || !trustStream ? pollAfter() : 0);
    }
  } finally {
    options.signal?.removeEventListener("abort", onAbort);
    if (owners.get(key) === owner) owners.delete(key);
  }
}
