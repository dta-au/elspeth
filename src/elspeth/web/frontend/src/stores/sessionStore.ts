// src/stores/sessionStore.ts
import { create } from "zustand";
import type {
  Session,
  ChatMessage,
  CompositionState,
  CompositionStateVersion,
  ComposerPreferences,
  ComposerProgressPhase,
  ComposerProgressSnapshot,
  CompositionProposal,
  ApiError,
  ComposerRecoveryError,
} from "@/types/api";
import { isComposerRecoveryError } from "@/types/recovery";
import type {
  SessionOperationRetryAcquisition,
  SessionOperationRetryHandle,
  SessionOperationRetryKind,
} from "./sessionOperationRetry";
import * as api from "@/api/client";
import {
  COMPOSE_TIMEOUT_ABORT_REASON,
  COMPOSE_USER_CANCEL_ABORT_REASON,
} from "@/config/composer";
import { useBlobStore } from "./blobStore";
import { useExecutionStore } from "./executionStore";
import { useInterpretationEventsStore } from "./interpretationEventsStore";
import { isGenuineReply } from "@/components/chat/turns";
import {
  acquireSessionOperationRetry,
  clearAllSessionOperationRetries,
  clearSessionOperationRetry,
  clearOrphanedSessionOperationRetriesForSession,
  isAmbiguousSessionOperationRetryFailure,
  isSessionOperationRetryReplayable,
} from "./sessionOperationRetry";


function getExecutionStore() {
  return useExecutionStore.getState();
}





const COMPOSER_PROGRESS_POLL_INTERVAL_MS = 1500;
const LLM_UNAVAILABLE_MESSAGE =
  "The AI service is temporarily unavailable. Please try again in a moment.";
const LLM_AUTH_ERROR_MESSAGE =
  "The AI service configuration is invalid. Please contact your administrator.";
// Surfaced when the client-side AbortController fires (typically the
// COMPOSE_TIMEOUT_MS guard in useComposer). Distinct from the backend's
// 422/convergence_wall_clock_timeout copy because the cause is different:
// the browser gave up before the server reached its own deadline.
const COMPOSE_TIMEOUT_MESSAGE =
  "ELSPETH took too long to compose a response. Try a smaller request or split it into multiple steps.";
const COMPOSE_CANCELLED_MESSAGE =
  "Composition stopped. You can revise your request and send it again.";
// The two turn-budget convergence causes: the model kept calling tools
// without settling, so the user's lever is a smaller request.
const CONVERGENCE_BUDGET_MESSAGE =
  "ELSPETH couldn't complete the composition after multiple attempts. Try breaking your request into smaller steps.";

/**
 * Copy for a 422 `error_type: "convergence"` failure (R2-F9,
 * elspeth-114dd261bc).
 *
 * A wall-clock timeout is a different event from a turn-budget exhaustion and
 * needs different copy: nothing was "attempted multiple times" — the clock ran
 * out — and the route handler has already persisted whatever pipeline the run
 * had built as a new composition-state version, which the next turn resumes
 * from. Saying otherwise sends the user off to rebuild work that still exists.
 *
 * Three honesty rules encoded here:
 *  - the elapsed budget is named only when the body reported it
 *    (`timeout_seconds`), never derived from the client's abort ceiling;
 *  - the saved-draft sentence appears only when a `partial_state` actually
 *    rode the response;
 *  - the backend's own `recovery_text` is appended rather than paraphrased,
 *    so the chat copy and the /composer-progress snapshot cannot drift.
 */
function formatConvergenceError(apiErr: ApiError): string {
  if (apiErr.reason !== "convergence_wall_clock_timeout") {
    return CONVERGENCE_BUDGET_MESSAGE;
  }
  const seconds = apiErr.timeout_seconds;
  const elapsed =
    typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0
      ? ` (${Math.round(seconds)}s)`
      : "";
  const outcome =
    apiErr.partial_state != null
      ? "Your partial pipeline was saved — continue from it or retry."
      : "No pipeline changes had been saved yet — retry, or try a smaller request.";
  const recovery =
    typeof apiErr.recovery_text === "string" ? apiErr.recovery_text.trim() : "";
  const headline = `ELSPETH ran out of time${elapsed}. ${outcome}`;
  return recovery ? `${headline} ${recovery}` : headline;
}

/**
 * Fold a convergence 422's salvaged draft into the store.
 *
 * The route handler saved `partial_state` as a NEW composition-state version
 * (provenance `convergence_persist`) and `get_current_state` returns the
 * highest version, so the partial IS what the next turn resumes from. Keeping
 * the pre-request graph on screen contradicts the server and the copy above.
 *
 * `recoveryStartedCompositionVersion` moves with it: that baseline exists to
 * catch a CONCURRENT third-party edit before RecoveryPanel's Apply overwrites
 * it, and this fold-in is neither concurrent nor third-party — leaving it
 * behind would make every timeout Apply raise a false alarm.
 */
function convergencePartialStatePatch(
  apiErr: ApiError,
  selectedNodeId: string | null,
): {
  compositionState?: CompositionState;
  selectedNodeId?: null;
  recoveryStartedCompositionVersion?: number;
} {
  const partial =
    apiErr.error_type === "convergence" ? apiErr.partial_state : null;
  if (partial == null) {
    return {};
  }
  const nodeStillExists =
    !selectedNodeId || partial.nodes.some((node) => node.id === selectedNodeId);
  return {
    compositionState: partial,
    recoveryStartedCompositionVersion: partial.version,
    ...(nodeStillExists ? {} : { selectedNodeId: null }),
  };
}
// Human names for the pending action in a live custody conflict — the copy
// must tell the user WHAT is unsettled, because "retry the same action" is
// only actionable when they know which action it means (session 09cde460:
// the anonymous copy left the operator guessing).
const SESSION_OPERATION_RETRY_KIND_LABELS: Record<SessionOperationRetryKind, string> = {
  state_revert: "version revert",
  session_fork: "session fork",
};

function sessionOperationRetryConflictState(kind: SessionOperationRetryKind): {
  error: string;
  errorDetails: null;
} {
  return {
    error:
      `Your earlier ${SESSION_OPERATION_RETRY_KIND_LABELS[kind]} is unsettled. ` +
      "Retry that same action to let it finish before choosing another.",
    errorDetails: null,
  };
}

// Reconcile a conflicting retry descriptor this page load cannot replay
// (session 09cde460: a reload mid-flight orphans the descriptor — the body
// existed only in the previous page's memory — and every DIFFERENT action
// then conflicted forever). An orphan has no custody value: clear it,
// best-effort refresh the authoritative state (the orphaned revert may
// have settled server-side), and let the caller's action proceed. A
// still-running server op is arbitrated by the backend admission gate, not
// by an unresolvable client block. Live custody
// (acquired by THIS page load, ambiguous-failure window) keeps today's
// blocking behavior — the exact replay it protects is really available.
async function reconcileOrphanedSessionOperationRetry(
  existing: SessionOperationRetryHandle,
  requestedSessionId: string,
): Promise<boolean> {
  if (isSessionOperationRetryReplayable(existing)) {
    return false;
  }
  clearSessionOperationRetry(existing);
  try {
    const [compositionState, sessions] = await Promise.all([
      api.fetchCompositionState(requestedSessionId),
      api.fetchSessions(),
    ]);
    if (useSessionStore.getState().activeSessionId === requestedSessionId) {
      useSessionStore.setState({
        compositionState,
        compositionStateLoaded: true,
        sessions,
      });
      getExecutionStore().clearValidation();
    }
  } catch {
    // Best-effort: the next action still revalidates against server authority.
  }
  return true;
}

// Conflict-path orphan reconciliation. Callers keep their SYNCHRONOUS
// acquire (the gate-check -> acquire -> pending-set sequence is the
// double-click mutual exclusion and must not gain an await on the fast
// path); only an actual conflict may go async. On an orphaned descriptor:
// reconcile, then re-acquire — the synchronous ledger is the tiebreaker if
// another action slipped in during the await (exactly one proceeds; the
// other gets a genuine live conflict). A surviving conflict is a real
// live-custody block the caller surfaces with named copy.
async function reconcileSessionOperationRetryConflict(
  existing: SessionOperationRetryHandle,
  kind: SessionOperationRetryKind,
  sessionId: string,
  requestIdentity: readonly unknown[],
): Promise<SessionOperationRetryAcquisition> {
  if (!(await reconcileOrphanedSessionOperationRetry(existing, sessionId))) {
    return { status: "conflict", existing };
  }
  return acquireSessionOperationRetry(kind, sessionId, requestIdentity);
}

function isAbortError(err: unknown): boolean {
  // DOMException ('AbortError'/'TimeoutError') is not always an Error
  // subclass across runtimes (browsers, jsdom, Node). Match on the
  // structural `name` field — that's the cross-platform contract.
  if (typeof err !== "object" || err === null) {
    return false;
  }
  const name = (err as { name?: unknown }).name;
  return name === "AbortError" || name === "TimeoutError";
}

function isComposeAbort(err: unknown): boolean {
  // abort() with NO argument rejects the fetch with a DOMException named
  // 'AbortError' — but useComposer aborts with a bare-string reason
  // (compose_timeout / compose_user_cancel), and per WHATWG semantics the
  // fetch then rejects with that RAW string. Classify on the rejection
  // value as well as the structural shape (elspeth-475647c47a).
  return (
    isAbortError(err) ||
    err === COMPOSE_TIMEOUT_ABORT_REASON ||
    err === COMPOSE_USER_CANCEL_ABORT_REASON
  );
}

/**
 * A fetch transport failure does not tell the browser whether the POST
 * reached the server.  Treating that failure as a definitive rejection leaves
 * the optimistic row marked retryable while the server may already have
 * persisted the user's message and advanced the pipeline.  Reconcile the
 * read-only session surfaces before offering a retry.
 *
 * HTTP errors are intentionally excluded: parseResponse received a response
 * and the route's structured error contract is authoritative for those
 * outcomes.  Abort errors have their own server-settlement resync above.
 */
function isAmbiguousComposeNetworkFailure(err: unknown): boolean {
  if (isComposeAbort(err) || typeof err !== "object" || err === null) {
    return false;
  }
  if (err instanceof TypeError) {
    return true;
  }
  const record = err as { name?: unknown; status?: unknown };
  return record.name === "NetworkError" || record.status === 0;
}

function abortReason(signal?: AbortSignal): unknown {
  return signal?.aborted === true ? signal.reason : undefined;
}

function composeAbortMessage(signal?: AbortSignal): string {
  return abortReason(signal) === COMPOSE_USER_CANCEL_ABORT_REASON
    ? COMPOSE_CANCELLED_MESSAGE
    : COMPOSE_TIMEOUT_MESSAGE;
}

/**
 * Refine the abort banner once the post-stop resync knows the durable head
 * (elspeth-2784531888). In auto_commit mode a Stop can land after committed
 * mutations; the generic abort copy ("revise your request and send it
 * again") then misrepresents what persisted. Composition-state versions are
 * allocated COALESCE(MAX(version), 0) + 1 per session (sessions/service.py),
 * so `resyncedVersion - (preTurnVersion ?? 0)` is an exact count of pipeline
 * changes the stopped turn saved.
 *
 * Returns null when the banner must not be rewritten: the current error is
 * not this turn's abort copy (a newer surface owns the banner), or the
 * resync learned nothing (state fetch returned no version) — a false "no
 * changes were saved" claim is worse than the generic copy.
 */
function stoppedComposeOutcomeMessage(
  currentError: string | null,
  preTurnVersion: number | null,
  resyncedVersion: number | null,
): string | null {
  if (resyncedVersion === null) return null;
  const isCancel = currentError === COMPOSE_CANCELLED_MESSAGE;
  const isTimeout = currentError === COMPOSE_TIMEOUT_MESSAGE;
  if (!isCancel && !isTimeout) return null;
  const saved = resyncedVersion - (preTurnVersion ?? 0);
  if (saved <= 0) {
    return isCancel
      ? "Composition stopped. No pipeline changes had been saved yet. You can revise your request and send it again."
      : "ELSPETH took too long to compose a response. No pipeline changes had been saved yet. Try a smaller request or split it into multiple steps.";
  }
  const changes =
    saved === 1 ? "1 pipeline change" : `${saved} pipeline changes`;
  const outcome = `${changes} had already been saved (now at version ${resyncedVersion}). Your next message continues from the saved draft.`;
  return isCancel
    ? `Composition stopped — ${outcome}`
    : `ELSPETH took too long to compose a response and was stopped — ${outcome}`;
}

function isHttpConflict(err: unknown): boolean {
  if (typeof err !== "object" || err === null) {
    return false;
  }
  return (err as { status?: unknown }).status === 409;
}



let composerProgressPollTimer: ReturnType<typeof setInterval> | null = null;
let composerProgressPollSessionId: string | null = null;
// True once THIS poll session (the span between startComposerProgressPolling
// and stopComposerProgressPolling) has observed a non-terminal snapshot.
// Guards against the stale-terminal-flash race: the immediate poll fired by
// startComposerProgressPolling can win the race against the POST's own
// "starting" publish and return the PRIOR turn's terminal snapshot
// (complete/failed/cancelled) still sitting in the registry. Surfacing that
// stale snapshot at the START of a fresh compose flashed the tutorial step-2
// indicator's LAST substep as current before dropping back to the first —
// exactly the backward-jump class the calling_model/using_tools remap was
// meant to prevent (elspeth-a8eeebb3aa review follow-up).
let composerProgressPollSeenNonTerminal = false;
// Ownership generations for the module-global pollers: each start* bumps
// its counter and returns it; stop* called with a stale generation no-ops.
// Prevents an aborted turn's delayed teardown (its settle wait yields the
// loop) from stopping the pollers a newer same-session turn now owns.
let composerProgressPollGeneration = 0;
let inflightMessagesPollGeneration = 0;
// Response ORDERING, which ownership generations cannot supply: both intervals
// launch a read without awaiting the previous one, so several reads of the
// same generation for the same session are in flight together and answer in
// whatever order the network returns them. Every one of them passes the
// ownership fence, so without this an older reply rolls the UI back over a
// newer one — progress regressing from "complete" to "using tools", or a
// message list erasing the reply the post-settle sync just brought in
// (polling audit 2026-09-22, finding 3). Each read takes a monotone ticket
// before its fetch and is applied only if no later ticket has been applied
// already; the counters are global because the pollers are.
let composerProgressReadTicket = 0;
let composerProgressAppliedTicket = 0;
let inflightMessagesReadTicket = 0;
let inflightMessagesAppliedTicket = 0;
let sessionPublicationGeneration = 0;

function advanceSessionPublicationGeneration(): number {
  sessionPublicationGeneration += 1;
  proposalSnapshotSequence += 1;
  return sessionPublicationGeneration;
}

/** Bind interpretation publication to the activation that displayed its card. */
export function createInterpretationResolutionHandler(
  sessionId: string,
): (newState: CompositionState | null) => void {
  const generation = sessionPublicationGeneration;
  return (newState: CompositionState | null): void => {
    const store = useSessionStore.getState();
    if (
      store.activeSessionId !== sessionId ||
      sessionPublicationGeneration !== generation
    ) return;
    store.applyResolvedInterpretation(newState);
  };
}

async function reconcileProposalConflict(
  sessionId: string,
  proposalId: string,
  error: ApiError,
  isCurrent: () => boolean,
): Promise<void> {
  if (!isCurrent()) return;
  const confirmedStaleBase = error.error_type === "proposal_base_state_changed";
  if (confirmedStaleBase && isCurrent()) {
    useSessionStore.setState((state) => ({
      staleProposalIds: Array.from(new Set([...state.staleProposalIds, proposalId])),
    }));
  }
  // Contention alone cannot retire a proposal. Only a named base refusal or
  // an authoritative lifecycle read may disable acceptance.
  const snapshot = beginProposalSnapshot();
  try {
    const proposals = await api.fetchCompositionProposals(sessionId);
    if (!isCurrent() || !snapshot.isCurrent()) return;
    useSessionStore.setState((state) => ({
      compositionProposals: snapshot.reconcile(state.compositionProposals, proposals),
      staleProposalIds: !confirmedStaleBase && proposals.some(
        (proposal) => proposal.id === proposalId && proposal.status === "pending",
      )
        ? state.staleProposalIds
        : Array.from(new Set([...state.staleProposalIds, proposalId])),
      error: error.detail ?? "The proposal could not be updated. Reload the session and try again.",
    }));
  } catch {
    if (isCurrent() && snapshot.isCurrent()) {
      useSessionStore.setState({
        error: error.detail ?? "The proposal could not be updated or refreshed. Reload the session.",
      });
    }
  }
}

function sessionPublicationIsCurrent(
  sessionId: string,
  generation: number,
): boolean {
  return (
    sessionPublicationGeneration === generation &&
    useSessionStore.getState().activeSessionId === sessionId
  );
}
const TERMINAL_COMPOSER_PROGRESS_PHASES = new Set<ComposerProgressPhase>([
  "complete",
  "failed",
  "cancelled",
]);

function clearComposerProgressPollTimer(): void {
  if (composerProgressPollTimer !== null) {
    clearInterval(composerProgressPollTimer);
    composerProgressPollTimer = null;
  }
  composerProgressPollSessionId = null;
}

// Live-message polling — runs while a send is inflight so the store can sync
// against assistant rows as the backend persists them. ChatPanel intentionally
// keeps incomplete agent turns hidden (see components/chat/turns.ts) and uses
// composerProgress as the live visible affordance until the final assistant
// text lands.
const INFLIGHT_MESSAGES_POLL_INTERVAL_MS = 1500;
let inflightMessagesPollTimer: ReturnType<typeof setInterval> | null = null;
let inflightMessagesPollSessionId: string | null = null;
// Latest inflight-poller claim generation per session. Unlike
// inflightMessagesPollSessionId this survives clearInflightMessagesPollTimer:
// a session switch stops the poller but does not abort the turn, so the
// owning turn's explicit sync needs to know whether a NEWER turn on the SAME
// session has claimed the poller since — not whether the timer still runs.
const inflightMessagesLatestClaimBySession = new Map<string, number>();

// A session can become active again while its earlier POST is unresolved.
// The poller's per-session claim also owns POST metadata and error publication;
// stopping its timer on navigation does not invalidate an unsuperseded turn.
function freeformComposeClaimIsCurrent(sessionId: string, generation: number): boolean {
  return (
    useSessionStore.getState().activeSessionId === sessionId &&
    inflightMessagesLatestClaimBySession.get(sessionId) === generation
  );
}

function clearInflightMessagesPollTimer(): void {
  if (inflightMessagesPollTimer !== null) {
    clearInterval(inflightMessagesPollTimer);
    inflightMessagesPollTimer = null;
  }
  inflightMessagesPollSessionId = null;
}

function formatProviderDiagnostic(apiErr: ApiError): string {
  const lines: string[] = [];
  if (apiErr.provider_detail) {
    lines.push(apiErr.provider_detail);
  }
  if (apiErr.provider_status_code !== undefined) {
    lines.push(`Provider status: ${apiErr.provider_status_code}`);
  }
  return lines.length > 0 ? `\n\n${lines.join("\n")}` : "";
}

function formatLlmUnavailableError(apiErr: ApiError): string {
  return `${apiErr.guidance ?? LLM_UNAVAILABLE_MESSAGE}${formatProviderDiagnostic(apiErr)}`;
}

function formatLlmAuthError(apiErr: ApiError): string {
  return `${LLM_AUTH_ERROR_MESSAGE}${formatProviderDiagnostic(apiErr)}`;
}

/**
 * F-4b: the fail-closed audit-integrity 500 refuses to REPLY — it does not
 * mean the message was lost. Every send_message path inserts the user row
 * before any audit-guard raise site, so "your message was saved" is honest
 * for this error_type; claiming anything about pipeline state is not.
 */
function formatAuditIntegrityError(apiErr: ApiError): string {
  const reference =
    apiErr.request_id === undefined
      ? " If this repeats, contact your administrator."
      : ` If this repeats, contact your administrator and reference request ID ${apiErr.request_id}.`;
  return (
    "ELSPETH stopped before replying because it could not verify this session's audit trail. " +
    "Your message was saved. Reload the session to see exactly what was stored." +
    reference
  );
}

async function refreshInterpretationEventsForSession(
  sessionId: string,
): Promise<void> {
  await useInterpretationEventsStore.getState().refreshAll(sessionId);
}

// Settle-wait pacing for resyncAfterAbortedComposeTurn. Deliberately NO
// wall-clock budget: synchronous tools are cancel-safe by running to
// completion (tool_batch.py — never wrapped in asyncio.wait_for), so the
// shielded window is unbounded by design and any time budget here would
// reintroduce the reconciliation race past its edge. The wait is bounded
// SEMANTICALLY instead — see waitForCancelledComposeToSettle.
const ABORT_RESYNC_SETTLE_POLL_MS = 500;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForCancelledComposeToSettle(
  sessionId: string,
  ownerGeneration: number,
): Promise<void> {
  for (;;) {
    const current = useSessionStore.getState();
    if (
      current.activeSessionId !== sessionId ||
      composerProgressPollGeneration !== ownerGeneration
    ) {
      return;
    }
    let inflightRequests: number;
    try {
      const snapshot = await api.fetchComposerProgress(sessionId);
      inflightRequests = snapshot.inflight_requests ?? 0;
    } catch {
      // Progress is advisory — settle best-effort and resync now.
      return;
    }
    if (inflightRequests === 0) {
      return;
    }
    await sleep(ABORT_RESYNC_SETTLE_POLL_MS);
  }
}

/**
 * INVARIANT (elspeth-06a23adfcc): every freeform compose entry point that
 * can be aborted client-side (sendMessage, retryMessage) MUST call this from
 * its abort branch. A client-side abort (Stop button / COMPOSE_TIMEOUT_MS
 * guard) only rejects the local fetch — the server turn keeps mutating the
 * session until the disconnect watcher cancels it, and every step it
 * completed before the cancel (canonical user row, assistant rows,
 * composition-state advances, proposals, interpretation reviews, blobs) is
 * committed by per-operation transactions, with the shielded in-flight
 * tool's P4 publish landing shortly AFTER the client's fetch has rejected
 * (see waitForCancelledComposeToSettle). The route's cancelled unwind then
 * persists only LLM-call telemetry, so waiting for terminal progress and
 * resyncing once observes everything renderable. Without this the
 * transcript/side rail keep the pre-send snapshot until a manual reload
 * (stale "No pipeline yet" at v1 while the server head is v3 with pending
 * review cards).
 *
 * Best-effort: the abort copy is already on screen, so a refetch failure
 * keeps the stale snapshot rather than stacking a second error on top.
 */
async function resyncAfterAbortedComposeTurn(
  sessionId: string,
  ownerGeneration: number,
  inflightOwnerGeneration: number,
  preTurnVersion: number | null,
): Promise<void> {
  const superseded = () =>
    useSessionStore.getState().activeSessionId !== sessionId ||
    composerProgressPollGeneration !== ownerGeneration;
  await waitForCancelledComposeToSettle(sessionId, ownerGeneration);
  if (superseded()) {
    // The wait exited because the resync became moot (the user navigated
    // away) or because a newer turn owns the state sync now — abandon
    // instead of fetching results only to discard them.
    return;
  }
  // Reuse the inflight reconciler: it drops the optimistic local-* row only
  // when its canonical counterpart was actually persisted, so a request
  // that never reached the route keeps its failed row + retry affordance.
  await useSessionStore
    .getState()
    .loadInflightMessages(sessionId, inflightOwnerGeneration);
  let state: CompositionState | null | undefined;
  let proposals: CompositionProposal[] | null | undefined;
  try {
    [state, proposals] = await Promise.all([
      api.fetchCompositionState(sessionId),
      api.fetchCompositionProposals(sessionId),
    ]);
  } catch {
    return;
  }
  if (superseded()) {
    // A newer turn started (and possibly finished) while the GETs were in
    // flight — this snapshot is stale against its results. Drop it.
    return;
  }
  useSessionStore.setState((s) => {
    const previousVersion = s.compositionState?.version ?? null;
    const newVersion = state?.version ?? null;
    const versionChanged =
      newVersion !== null && newVersion !== previousVersion;
    // R4-H3 mirror of the success branches: a new state version invalidates
    // any validation verdict rendered against the old one.
    if (versionChanged) {
      getExecutionStore().clearValidation();
    }
    const newState = state ?? s.compositionState;
    const nodeStillExists =
      !s.selectedNodeId ||
      newState?.nodes.some((n) => n.id === s.selectedNodeId);
    // Now that the durable head is known, replace the generic abort copy
    // with what actually persisted (elspeth-2784531888). Exact-match on the
    // current error keeps this from clobbering any newer banner.
    const refinedError = stoppedComposeOutcomeMessage(
      s.error,
      preTurnVersion,
      state?.version ?? null,
    );
    return {
      compositionState: newState,
      compositionProposals: proposals ?? s.compositionProposals,
      ...(refinedError !== null ? { error: refinedError } : {}),
      ...(nodeStillExists ? {} : { selectedNodeId: null }),
    };
  });
  // Same fire-and-forget refreshes as the success branches: the cancelled
  // turn may have created blobs, auto-titled the session, and minted
  // interpretation reviews before the cancel landed.
  useBlobStore.getState().loadBlobs(sessionId);
  void useSessionStore.getState().loadSessions();
  void refreshInterpretationEventsForSession(sessionId);
}

/**
 * Reconcile a freeform turn whose POST result was lost at the transport
 * boundary.  The request may still be running after fetch rejects, so wait for
 * the same semantic quiescence used by the abort path before reading the
 * durable message/state/proposal surfaces.  This helper performs no write and
 * never replays the user's request.
 *
 * `baselineMessageIds` lets the caller distinguish a reply created by this
 * turn from messages that were already visible.  When no new durable evidence
 * is found, the failed-row retry contract remains in place; a later explicit
 * retry is then the only path that can issue another POST.
 */
async function resyncAfterAmbiguousComposeFailure(
  sessionId: string,
  ownerGeneration: number,
  inflightOwnerGeneration: number,
  baselineVersion: number | null,
  baselineMessageIds: ReadonlySet<string>,
  messageId: string,
  messageContent: string,
  clientRequestId?: string,
): Promise<void> {
  const superseded = () =>
    useSessionStore.getState().activeSessionId !== sessionId ||
    composerProgressPollGeneration !== ownerGeneration;
  await waitForCancelledComposeToSettle(sessionId, ownerGeneration);
  if (superseded()) return;

  const freshMessages = await useSessionStore
    .getState()
    .loadInflightMessages(sessionId, inflightOwnerGeneration);
  if (superseded()) return;
  if (clientRequestId !== undefined) {
    const accepted = freshMessages?.find((message) =>
      message.role === "user" && message.client_request_id === clientRequestId
    );
    if (accepted) {
      await reconcileAcceptedSend(
        sessionId, messageId, clientRequestId, accepted.id,
        inflightOwnerGeneration, ownerGeneration,
      );
      return;
    }
  }

  let state: CompositionState | null | undefined;
  let proposals: CompositionProposal[] | null | undefined;
  const [stateResult, proposalsResult] = await Promise.allSettled([
    api.fetchCompositionState(sessionId),
    api.fetchCompositionProposals(sessionId),
  ]);
  if (stateResult.status === "fulfilled") state = stateResult.value;
  if (proposalsResult.status === "fulfilled") proposals = proposalsResult.value;
  if (superseded()) return;

  const newDurableUser =
    freshMessages?.some(
      (message) =>
        !baselineMessageIds.has(message.id) &&
        message.role === "user" &&
        (clientRequestId !== undefined
          ? message.client_request_id === clientRequestId
          : message.content === messageContent),
    ) ?? false;
  const newAssistant = freshMessages?.some((message) =>
    !baselineMessageIds.has(message.id) && message.role === "assistant"
  ) ?? false;
  const stateAdvanced = state?.version != null && state.version > (baselineVersion ?? 0);
  // State advances and assistant rows can belong to another turn. The
  // acceptance identity is the only proof that a new POST was saved.
  const durableEvidence = clientRequestId !== undefined
    ? newDurableUser
    : newDurableUser || newAssistant || stateAdvanced;

  useSessionStore.setState((s) => {
    const previousVersion = s.compositionState?.version ?? null;
    const newVersion = state?.version ?? null;
    if (newVersion !== null && newVersion !== previousVersion) {
      getExecutionStore().clearValidation();
    }
    const newState = state ?? s.compositionState;
    const nodeStillExists =
      !s.selectedNodeId ||
      newState?.nodes.some((node) => node.id === s.selectedNodeId);
    const repairedMessages = durableEvidence
      ? s.messages.map((message) =>
          message.id === messageId
            ? {
                ...message,
                local_status: undefined,
                local_error: undefined,
                local_failure_code: undefined,
              }
            : message,
        )
      : s.messages;
    return {
      compositionState: newState,
      compositionProposals: proposals ?? s.compositionProposals,
      ...(durableEvidence
        ? {
            messages: repairedMessages,
            error:
              "Your request was saved, but its response could not be confirmed. The latest messages and pipeline state are shown; reload if anything looks missing.",
          }
        : {}),
      ...(nodeStillExists ? {} : { selectedNodeId: null }),
    };
  });
  useBlobStore.getState().loadBlobs(sessionId);
  void useSessionStore.getState().loadSessions();
  void refreshInterpretationEventsForSession(sessionId);
}

/** Reconcile an ingress receipt without ever starting a second model call. */
async function reconcileAcceptedSend(
  sessionId: string,
  localMessageId: string,
  clientRequestId: string,
  canonicalUserMessageId: string,
  inflightGeneration: number,
  progressGeneration: number,
): Promise<void> {
  const current = () =>
    freeformComposeClaimIsCurrent(sessionId, inflightGeneration) &&
    composerProgressPollGeneration === progressGeneration;
  // Stop both periodic readers first. A progress tick that starts while a
  // slow state/proposal read is pending would otherwise claim a later ticket
  // and make the receipt's complete four-surface snapshot look stale.
  useSessionStore.getState().stopInflightMessagesPolling(sessionId, inflightGeneration);
  useSessionStore.getState().stopComposerProgressPolling(sessionId, progressGeneration);
  const messageTicket = ++inflightMessagesReadTicket;
  const progressTicket = ++composerProgressReadTicket;
  const proposalSnapshot = beginProposalSnapshot();
  const stateAtDispatch = useSessionStore.getState().compositionState;
  const stateLoadedAtDispatch = useSessionStore.getState().compositionStateLoaded;
  const acceptedDecisionAtDispatch = acceptedProposalDecisionSequence;
  try {
    const [messages, state, proposals, progress] = await Promise.all([
      api.fetchMessages(sessionId),
      api.fetchCompositionState(sessionId),
      api.fetchCompositionProposals(sessionId),
      api.fetchComposerProgress(sessionId),
    ]);
    if (!current()) return;
    if (!messages.some((message) =>
      message.id === canonicalUserMessageId &&
      message.role === "user" &&
      message.client_request_id === clientRequestId
    )) {
      throw new Error("Accepted message is not yet visible in the transcript");
    }
    if (messageTicket <= inflightMessagesAppliedTicket || progressTicket <= composerProgressAppliedTicket) {
      throw new Error("A newer session snapshot owns the display");
    }
    inflightMessagesAppliedTicket = messageTicket;
    composerProgressAppliedTicket = progressTicket;
    useSessionStore.setState((s) => {
      if (!current()) return s;
      const localIntent = s.messages.find((message) =>
        message.id === localMessageId ||
        (message.role === "user" && message.client_request_id === clientRequestId)
      );
      const acceptedIndex = messages.findIndex((message) => message.id === canonicalUserMessageId);
      const isLastUser = !messages.slice(acceptedIndex + 1).some((message) => message.role === "user");
      const hasReply = hasGenuineReplyForUser(messages, acceptedIndex);
      const canDeliberatelyRetry = isLastUser && !hasReply &&
        (progress.inflight_requests ?? 0) === 0 &&
        (progress.phase === "idle" || TERMINAL_COMPOSER_PROGRESS_PHASES.has(progress.phase));
      const reconciledMessages = canDeliberatelyRetry
        ? messages.map((message) => message.id === canonicalUserMessageId
          ? {
              ...message,
              local_status: "failed" as const,
              local_error: "Your message was saved, but no reply is available. Retry to request a response.",
              local_failure_code: localIntent?.local_failure_code,
            }
          : message)
        : messages;
      // A proposal acceptance can replace the pipeline while these four
      // reads are pending. Its receipt and hydration own that newer state.
      const stateSuperseded = acceptedProposalDecisionSequence !== acceptedDecisionAtDispatch ||
        s.compositionState !== stateAtDispatch ||
        s.compositionStateLoaded !== stateLoadedAtDispatch;
      const nextState = stateSuperseded ? s.compositionState : state ?? s.compositionState;
      const previousVersion = s.compositionState?.version ?? null;
      if (!stateSuperseded && nextState?.version != null && nextState.version !== previousVersion) {
        getExecutionStore().clearValidation();
      }
      const nodeStillExists = !s.selectedNodeId ||
        nextState?.nodes.some((node) => node.id === s.selectedNodeId);
      return {
        messages: reconciledMessages,
        compositionState: nextState,
        compositionProposals: proposalSnapshot.reconcile(s.compositionProposals, proposals),
        composerProgress: progress.phase === "idle" ? null : progress,
        isComposing: false,
        error: canDeliberatelyRetry
          ? "Your message was saved without a reply. Retry the saved message to request a response."
          : "Your message was saved. The latest session state is shown.",
        ...(nodeStillExists ? {} : { selectedNodeId: null }),
      };
    });
    useBlobStore.getState().loadBlobs(sessionId);
    void useSessionStore.getState().loadSessions();
    void refreshInterpretationEventsForSession(sessionId);
  } catch {
    if (!current()) return;
    useSessionStore.setState((s) => ({
      isComposing: false,
      error: "Your message was saved, but the latest session state could not be confirmed. Retry to refresh it; this will not resend the message.",
      messages: s.messages.map((message) =>
        message.id === localMessageId ||
        (message.role === "user" && message.client_request_id === clientRequestId)
        ? {
            ...message,
            local_status: "failed",
            local_error: "Message saved; retry to refresh the session without resending.",
            local_accepted_user_message_id: canonicalUserMessageId,
          }
        : message),
    }));
  }
}


function mergeCompositionProposals(
  existing: CompositionProposal[],
  incoming: CompositionProposal[],
): CompositionProposal[] {
  if (incoming.length === 0) {
    return existing;
  }
  const byId = new Map(existing.map((proposal) => [proposal.id, proposal]));
  for (const proposal of incoming) {
    const previous = byId.get(proposal.id);
    if (previous && previous.status !== "pending" && proposal.status === "pending") continue;
    byId.set(proposal.id, proposal);
  }
  return Array.from(byId.values());
}

let proposalSnapshotSequence = 0;
let acceptedProposalDecisionSequence = 0;

/** A tool-call row is narration; only the final answer closes this user turn. */
function hasGenuineReplyForUser(messages: ChatMessage[], userIndex: number): boolean {
  for (let index = userIndex + 1; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role === "user") break;
    if (message.role === "assistant" && message.content.length > 0 && isGenuineReply(message)) {
      return true;
    }
  }
  return false;
}

/** A list read may retire only proposals it knew about when dispatched. */
function beginProposalSnapshot(): {
  isCurrent: () => boolean;
  reconcile: (existing: CompositionProposal[], snapshot: CompositionProposal[]) => CompositionProposal[];
} {
  const sequence = ++proposalSnapshotSequence;
  const knownIds = new Set(useSessionStore.getState().compositionProposals.map((proposal) => proposal.id));
  return {
    isCurrent: () => sequence === proposalSnapshotSequence,
    reconcile: (existing, snapshot) => {
      if (sequence !== proposalSnapshotSequence) return existing;
      return mergeCompositionProposals(
        existing.filter((proposal) => proposal.status !== "pending" || !knownIds.has(proposal.id)),
        snapshot,
      );
    },
  };
}








function mergeAuthoritativeForkChild(
  current: Session[],
  authoritative: Session[],
  childSessionId: string,
): Session[] {
  const child = authoritative.find((session) => session.id === childSessionId);
  if (child === undefined) {
    throw new Error("Fork result was absent from the authoritative session list");
  }
  // Preserve every intervening unrelated list mutation. A fetch started before
  // a rename, create, or archive is authoritative only for the exact committed
  // locator child. Put that fresh child first to preserve the backend's
  // updated_at DESC contract without admitting stale values for other rows.
  return [child, ...current.filter((session) => session.id !== childSessionId)];
}

function clearedRecoveryState(): Pick<
  SessionState,
  "recoveryError" | "recoveryStartedCompositionVersion"
> {
  return {
    recoveryError: null,
    recoveryStartedCompositionVersion: null,
  };
}

interface ApplyRecoveredStateOptions {
  confirmed?: boolean;
}

interface ApplyRecoveredStateResult {
  applied: boolean;
  needsConfirmation: boolean;
}

/**
 * The `source_blob_ids` sidecar from the most recent YAML export fetch,
 * paired with the exact YAML it describes and the session it belongs to.
 *
 * Blob refs are session-scoped (the import endpoint 404s a foreign-session
 * blob), and the import handler 400s a sidecar entry naming a source absent
 * from the pasted YAML. So ImportYamlModal replays this ONLY when both guards
 * hold: `sessionId` still matches the active session AND `yaml` matches the
 * pasted text verbatim. Those two checks make a stale binding inert, which is
 * why it is not threaded through every session-reset site — a mismatch simply
 * declines to replay, and the backend then asks the user to re-provide.
 */
export interface ExportedYamlBlobBinding {
  sessionId: string;
  yaml: string;
  sourceBlobIds: Record<string, string>;
}

interface SessionState {
  sessions: Session[];
  /**
   * True once loadSessions has resolved successfully at least once.
   * Consumers (returning-user auto-resume, the no-sessions empty landing)
   * must not act on an EMPTY sessions array before the list has actually
   * loaded — an unfetched list and a genuinely empty account look identical
   * without this flag.
   */
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  messages: ChatMessage[];
  compositionState: CompositionState | null;
  /**
   * Whether the LAST completed freeform compose turn changed the
   * composition-state version (elspeth-bf9c296ee5). `null` means unknown: no
   * compose turn has completed for this session view yet, or one is in
   * flight. The terminal completion badge derives "Response ready" vs
   * "Pipeline updated" from this — it is the persisted form of the
   * `versionChanged` comparison the compose success branches already make
   * (previously computed only to clear stale validation, then discarded).
   */
  lastComposeChangedPipeline: boolean | null;
  /**
   * True once the active session's composition state is KNOWN — i.e. the
   * selectSession fetch settled (success, 404, or failure), or the session
   * was just created/forked (fresh state is known by construction).
   * `compositionState === null` alone is ambiguous: it means both "still
   * fetching" and "loaded, and this session has no pipeline yet". The
   * #/{id}/yaml hash route gates the Export-YAML modal on content
   * (elspeth-bff8043d33) and needs the disambiguation to avoid either
   * breaking the deep link or opening the modal on an empty pipeline.
   */
  compositionStateLoaded: boolean;
  compositionProposals: CompositionProposal[];
  /**
   * source_blob_ids sidecar captured on the last export fetch, so a
   * same-session verbatim re-import can rebind blob-backed sources. Null
   * until an export is fetched (and reset to null by an export with no
   * blob-backed source). See ExportedYamlBlobBinding for the replay guards.
   */
  exportedYamlBlobBinding: ExportedYamlBlobBinding | null;
  setExportedYamlBlobBinding: (binding: ExportedYamlBlobBinding | null) => void;
  composerPreferences: ComposerPreferences | null;
  staleProposalIds: string[];
  proposalActionPendingIds: string[];
  composerProgress: ComposerProgressSnapshot | null;
  isComposing: boolean;
  composeTimeoutReady: boolean;
  setComposeTimeoutReady: (ready: boolean) => void;
  /**
   * TRUE when the backend is reachable (GET /api/system/status returned) but
   * did NOT supply a usable composer_timeout_seconds, so composeTimeoutReady
   * can never latch. Distinguishes "still booting" (both false) from "up but
   * misconfigured" (this true) so the Send affordances can show a distinct
   * diagnostic instead of a perpetual "Connecting…". Reset to false whenever a
   * valid ceiling lands or the backend goes unreachable.
   */
  composerTimeoutUnavailable: boolean;
  setComposerTimeoutUnavailable: (unavailable: boolean) => void;
  /**
   * Deployment-level composer model identity (ELSPETH_WEB__COMPOSER_MODEL),
   * written by App's health poll — the single /api/system/status consumer —
   * and read by the AppHeader ModelChip. NULL until a successful poll
   * reports a non-empty model; a later failed poll does not clear it (the
   * fact is deployment configuration, not liveness — the backend banner
   * owns unreachability).
   */
  composerModel: string | null;
  setComposerModel: (model: string | null) => void;
  /**
   * Deployment-level advisor model identity
   * (ELSPETH_WEB__COMPOSER_ADVISOR_MODEL) — the model that gates completion —
   * written by the same health poll beside composerModel and read by the
   * AppHeader ModelChip. NULL until the first successful status poll
   * publishes it; a later failed poll does not clear it.
   */
  composerAdvisorModel: string | null;
  setComposerAdvisorModel: (model: string | null) => void;
  stateVersions: CompositionStateVersion[];
  error: string | null;
  /**
   * Optional structured detail rows rendered as bullet points beneath
   * `error` in the error banner. Populated when an ApiError carries
   * structured `validation_errors` — currently set by `acceptProposal`
   * on a 422 `proposal_validation_failed` response. Cleared whenever
   * `error` is cleared.
   */
  errorDetails: string[] | null;
  recoveryError: ComposerRecoveryError | null;
  recoveryStartedCompositionVersion: number | null;

  // Shared selection state for GraphView component focus.
  selectedNodeId: string | null;
  selectNode: (nodeId: string | null) => void;

  /**
   * One-shot request for ComposerWorkspace to un-collapse the authoring
   * pane and focus the chat input. Set by createSession (a new session's
   * composer must never open hidden behind the globally-persisted collapsed
   * preference); consumed by ComposerWorkspace on mount or change via
   * consumeAuthoringFocusRequest. A store flag rather than a window event
   * because createSession can run while the workspace is unmounted (empty
   * landing, tutorial graduation) — an event there has zero listeners.
   */
  authoringFocusRequested: boolean;
  consumeAuthoringFocusRequest: () => void;

  loadSessions: () => Promise<void>;
  createSession: () => Promise<void>;
  selectSession: (id: string) => Promise<void>;
  resetForTutorialSession: (sessionId: string) => void;
  /**
   * Release the active-session binding when `sessionId` turns out not to
   * exist server-side (dead tutorial resume). Guarded: a no-op unless
   * `activeSessionId` still equals `sessionId`, so a recovery that races a
   * legitimate re-bind can never blank the new session. Without this, the
   * tutorial's dead-resume recovery resets the tutorial machine but leaves
   * the store bound to the dead id — and every consumer keyed on
   * `activeSessionId` (InlineRunResults' run list, composer progress)
   * keeps 404-ing against a session that will never come back.
   */
  unbindMissingSession: (sessionId: string) => void;
  renameSession: (id: string, title: string) => Promise<void>;
  archiveSession: (id: string) => Promise<void>;
  sendMessage: (content: string, signal?: AbortSignal, retryLocalMessageId?: string) => Promise<void>;
  loadCompositionProposals: (sessionId?: string) => Promise<void>;
  acceptProposal: (proposalId: string) => Promise<void>;
  rejectProposal: (proposalId: string) => Promise<void>;
  /**
   * Omit ownerGeneration for the interval tick (fenced on the live poller
   * claim, like loadInflightMessages). The owning turn's explicit post-stop
   * read passes the generation its startComposerProgressPolling returned —
   * that read runs AFTER the poller was deliberately stopped, so the stop is
   * not a reason to drop it; only a newer turn claiming the poller is.
   */
  loadComposerProgress: (
    sessionId?: string,
    options?: { discardStaleTerminal?: boolean; ownerGeneration?: number },
  ) => Promise<void>;
  /**
   * The pollers are MODULE-GLOBAL singletons, so ownership must be
   * explicit: start* returns a generation token, and stop* with that token
   * no-ops when a newer turn has since claimed the poller. Without the
   * token, an aborted turn whose settle wait outlived it would tear down
   * the pollers of the turn the user started in the meantime.
   */
  startComposerProgressPolling: (sessionId: string) => number;
  stopComposerProgressPolling: (sessionId?: string, generation?: number) => void;
  /**
   * Omit ownerGeneration for the interval tick (fenced on the live poller
   * claim). The owning turn's explicit sync passes the generation its
   * startInflightMessagesPolling returned, so a session switch that stopped
   * the poller does not drop it.
   */
  loadInflightMessages: (
    sessionId: string,
    ownerGeneration?: number,
  ) => Promise<ChatMessage[] | null>;
  startInflightMessagesPolling: (sessionId: string) => number;
  stopInflightMessagesPolling: (sessionId?: string, generation?: number) => void;
  retryMessage: (messageId: string, signal?: AbortSignal) => Promise<void>;
  forkFromMessage: (messageId: string, newContent: string) => Promise<void>;
  openRecoveryFromError: (
    error: ApiError,
    recoveryStartedCompositionVersion: number | null,
  ) => boolean;
  applyRecoveredState: (
    options?: ApplyRecoveredStateOptions,
  ) => ApplyRecoveredStateResult;
  discardRecovery: () => void;
  loadStateVersions: () => Promise<void>;
  isLoadingVersions: boolean;
  revertToVersion: (stateId: string) => Promise<void>;
  applyResolvedInterpretation: (newState: CompositionState | null) => void;

  clearError: () => void;
  injectSystemMessage: (content: string, stableId?: string) => void;
  reset: () => void;
}

const initialState = {
  sessions: [] as Session[],
  sessionsLoaded: false,
  activeSessionId: null as string | null,
  messages: [] as ChatMessage[],
  compositionState: null as CompositionState | null,
  lastComposeChangedPipeline: null as boolean | null,
  compositionStateLoaded: false,
  compositionProposals: [] as CompositionProposal[],
  exportedYamlBlobBinding: null as ExportedYamlBlobBinding | null,
  composerPreferences: null as ComposerPreferences | null,
  staleProposalIds: [] as string[],
  proposalActionPendingIds: [] as string[],
  composerProgress: null as ComposerProgressSnapshot | null,
  isComposing: false,
  composeTimeoutReady: false,
  composerTimeoutUnavailable: false,
  composerModel: null as string | null,
  composerAdvisorModel: null as string | null,
  stateVersions: [] as CompositionStateVersion[],
  isLoadingVersions: false,
  error: null as string | null,
  errorDetails: null as string[] | null,
  selectedNodeId: null as string | null,
  authoringFocusRequested: false,
  ...clearedRecoveryState(),
};

export const useSessionStore = create<SessionState>((set, get) => ({
  ...initialState,

  setExportedYamlBlobBinding(binding) {
    set({ exportedYamlBlobBinding: binding });
  },

  setComposeTimeoutReady(ready) {
    set({ composeTimeoutReady: ready });
  },

  setComposerTimeoutUnavailable(unavailable) {
    set({ composerTimeoutUnavailable: unavailable });
  },

  setComposerModel(model) {
    set({ composerModel: model });
  },

  setComposerAdvisorModel(model) {
    set({ composerAdvisorModel: model });
  },

  async loadSessions() {
    // Fetch-generation guard (elspeth-4d5b0e634a): every store action that
    // mutates `sessions` (createSession, renameSession, archiveSession,
    // forkFromMessage, reset) — and any external
    // `useSessionStore.setState()` caller, e.g. HelloWorldTutorial's
    // post-rename merge — replaces the array with a brand-new reference;
    // none of them mutate it in place. That makes the
    // reference itself a cheap monotonic generation marker: capture it
    // before the fetch, and if it has changed by the time the fetch
    // resolves, something newer already landed while this request was in
    // flight. Applying the snapshot we just fetched would silently clobber
    // that newer state — e.g. the app-start loadSessions racing the
    // tutorial's createSession+renameSession and overwriting the renamed
    // session with its own pre-rename snapshot. Bail without touching
    // `sessions` in that case.
    const sessionsBeforeFetch = get().sessions;
    try {
      const sessions = await api.fetchSessions();
      if (get().sessions !== sessionsBeforeFetch) {
        // Stale response — the list already reflects newer state. The
        // fetch itself still succeeded, so the loaded-ness flag is honest
        // to flip even though we're discarding this particular snapshot.
        set({ sessionsLoaded: true });
        return;
      }
      set({ sessions, sessionsLoaded: true });
    } catch {
      if (get().sessions !== sessionsBeforeFetch) {
        // A newer, non-fetch mutation already superseded this snapshot;
        // a failed background refresh shouldn't surface an alarming error
        // banner for state the user's own actions have already moved past.
        return;
      }
      set({ error: "Failed to load sessions. Please refresh the page." });
    }
  },

  async createSession() {
    let session;
    try {
      session = await api.createSession();
    } catch {
      set({ error: "Failed to create session. Please try again." });
      return;
    }
    clearComposerProgressPollTimer();
    clearInflightMessagesPollTimer();
    advanceSessionPublicationGeneration();
    useBlobStore.getState().activateSession(session.id);
    set((state) => ({
      sessions: [session, ...state.sessions],
      activeSessionId: session.id,
      messages: [],
      // A freshly created session is KNOWN to have no composition state.
      compositionState: null,
      compositionStateLoaded: true,
      compositionProposals: [],
      composerPreferences: null,
      staleProposalIds: [],
      proposalActionPendingIds: [],
      composerProgress: null,
      stateVersions: [],
      error: null,
      selectedNodeId: null, // Clear selection for new session
      // A collapsed authoring pane is a GLOBAL persisted preference
      // (useWorkspacePaneState's localStorage layout), so without this a
      // user who collapsed the pane once would find every NEW session
      // opening with the composer — the primary authoring surface — hidden
      // (2026-08-15 UX review). A STORE FLAG, not a window event:
      // createSession can run while ComposerWorkspace is unmounted (the
      // empty-landing "+ New session" button, tutorial graduation), where a
      // dispatched event lands on zero listeners and the request is lost.
      // ComposerWorkspace consumes the flag on mount or change,
      // un-collapsing the pane and focusing the chat input. Session
      // SWITCHES deliberately do not set it: revisiting an existing session
      // honours the standing collapsed preference.
      authoringFocusRequested: true,
      ...clearedRecoveryState(),
    }));
  },

  async archiveSession(id: string) {
    try {
      await api.archiveSession(id);
      const wasActive = get().activeSessionId === id;
      if (wasActive) {
        clearComposerProgressPollTimer();
        clearInflightMessagesPollTimer();
        advanceSessionPublicationGeneration();
        useBlobStore.getState().activateSession(null);
      }
      set((state) => {
        const sessions = state.sessions.filter((s) => s.id !== id);
        // If we archived the active session, clear selection
        return {
          sessions,
          ...(wasActive
            ? {
                activeSessionId: null,
                messages: [],
                compositionState: null,
                compositionStateLoaded: false,
                compositionProposals: [],
                composerPreferences: null,
                staleProposalIds: [],
                proposalActionPendingIds: [],
                composerProgress: null,
                stateVersions: [],
                isComposing: false,
                selectedNodeId: null,
                ...clearedRecoveryState(),
              }
            : {}),
        };
      });
    } catch (err) {
      // Preserve the original error for callers that want to surface
      // ``err.message`` inline (HeaderSessionSwitcher renders an inline
      // role="alert" co-located with the trigger).  We also set the
      // composer-level fallback so the global error region stays useful
      // if no inline handler is wired.  Re-throwing lets the component
      // catch keep the diagnostic detail without losing the fallback.
      set({ error: "Failed to archive session. Please try again." });
      throw err;
    }
  },

  async renameSession(id: string, title: string) {
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      const session = await api.renameSession(id, trimmed);
      set((state) => ({
        sessions: state.sessions.map((existing) =>
          existing.id === id ? session : existing,
        ),
        error: null,
      }));
    } catch (err) {
      // Same pattern as ``archiveSession`` above — re-raise so an inline
      // handler can preserve ``err.message`` while the global error
      // region still receives a friendly fallback.
      set({ error: "Failed to rename session. Please try again." });
      throw err;
    }
  },

  async selectSession(id: string) {
    getExecutionStore().clearValidation();
    clearComposerProgressPollTimer();
    clearInflightMessagesPollTimer();
    // Activation's authoritative reads supersede orphaned browser descriptors.
    clearOrphanedSessionOperationRetriesForSession(id);
    const selectionGeneration = advanceSessionPublicationGeneration();

    useBlobStore.getState().activateSession(id);
    set({
      activeSessionId: id,
      messages: [],
      compositionState: null,
      lastComposeChangedPipeline: null,
      compositionStateLoaded: false,
      compositionProposals: [],
      composerPreferences: null,
      staleProposalIds: [],
      proposalActionPendingIds: [],
      composerProgress: null,
      stateVersions: [],
      isComposing: false,
      error: null,
      selectedNodeId: null,
      ...clearedRecoveryState(),
    });

    try {
      const [messages, compositionState, compositionProposals, composerPreferences] =
        await Promise.all([
          api.fetchMessages(id),
          api.fetchCompositionState(id),
          api.fetchCompositionProposals(id),
          api.fetchComposerPreferences(id),
        ]);
      if (!sessionPublicationIsCurrent(id, selectionGeneration)) return;
      set({
        messages,
        compositionState,
        compositionStateLoaded: true,
        compositionProposals: compositionProposals ?? [],
        composerPreferences: composerPreferences ?? null,
      });
      useBlobStore.getState().loadBlobs(id);
      void useInterpretationEventsStore.getState().refreshAll(id);
    } catch (err) {
      if ((err as ApiError).status === 404 && sessionPublicationIsCurrent(id, selectionGeneration)) {
        advanceSessionPublicationGeneration();
        useBlobStore.getState().activateSession(null);
        set({
          activeSessionId: null,
          messages: [],
          compositionState: null,
          compositionStateLoaded: false,
          compositionProposals: [],
          composerPreferences: null,
          staleProposalIds: [],
          proposalActionPendingIds: [],
          composerProgress: null,
          stateVersions: [],
          isComposing: false,
          error: null,
          selectedNodeId: null,
          ...clearedRecoveryState(),
        });
        return;
      }
      if (sessionPublicationIsCurrent(id, selectionGeneration)) {
        set({
          error: "Failed to load session. Please refresh the page.",
          compositionStateLoaded: true,
        });
      }
    }
  },

  resetForTutorialSession(sessionId: string) {
    advanceSessionPublicationGeneration();
    useBlobStore.getState().activateSession(sessionId);
    set({
      activeSessionId: sessionId,
      messages: [],
      compositionState: null,
      compositionStateLoaded: false,
      compositionProposals: [],
      composerPreferences: null,
      staleProposalIds: [],
      proposalActionPendingIds: [],
      composerProgress: null,
      stateVersions: [],
      isComposing: false,
      error: null,
      selectedNodeId: null,
      ...clearedRecoveryState(),
    });
  },

  unbindMissingSession(sessionId: string) {
    if (get().activeSessionId !== sessionId) {
      return;
    }
    clearComposerProgressPollTimer();
    clearInflightMessagesPollTimer();
    advanceSessionPublicationGeneration();
    useBlobStore.getState().activateSession(null);
    set({
      activeSessionId: null,
      messages: [],
      compositionState: null,
      compositionStateLoaded: false,
      compositionProposals: [],
      composerPreferences: null,
      staleProposalIds: [],
      proposalActionPendingIds: [],
      composerProgress: null,
      stateVersions: [],
      isComposing: false,
      error: null,
      selectedNodeId: null,
      ...clearedRecoveryState(),
    });
  },

  async sendMessage(content: string, signal?: AbortSignal, retryLocalMessageId?: string) {
    const { activeSessionId, isComposing } = get();
    if (!activeSessionId) return;
    // Synchronous admission gate (elspeth-3f38ebb1b5): exactly one freeform
    // compose may be in flight. isComposing is set synchronously below
    // before any await, so this check cannot race another entry point —
    // without it Retry / Use-as-input could start a second compose whose
    // AbortController displaced the first one's, leaving Stop owning only
    // the newest request. Entry surfaces are disabled while composing;
    // this gate is the invariant for programmatic callers.
    if (isComposing) return;
    const recoveryStartedCompositionVersion =
      get().compositionState?.version ?? null;
    const baselineMessageIds = new Set(get().messages.map((message) => message.id));

    const retriedIntent = retryLocalMessageId
      ? get().messages.find((message) => message.id === retryLocalMessageId && message.id.startsWith("local-"))
      : undefined;
    if (retryLocalMessageId && (!retriedIntent || !retriedIntent.client_request_id)) return;
    const stateId = retriedIntent
      ? retriedIntent.local_requested_state_id ?? null
      : get().compositionState?.id ?? null;
    const clientRequestId = retriedIntent?.client_request_id ?? crypto.randomUUID();
    const optimisticMessage: ChatMessage = retriedIntent ?? {
      id: `local-${clientRequestId}`,
      session_id: activeSessionId,
      role: "user",
      content,
      client_request_id: clientRequestId,
      local_requested_state_id: stateId,
      tool_calls: null,
      created_at: new Date().toISOString(),
      local_status: "pending",
    };

    set((state) => ({
      isComposing: true,
      error: null,
      composerProgress: null,
      // In flight the mutation verdict is unknown — a stale verdict from the
      // previous turn must not label this turn's completion badge.
      lastComposeChangedPipeline: null,
      messages: retriedIntent
        ? state.messages.map((message) => message.id === optimisticMessage.id
          ? { ...message, local_status: "pending", local_error: undefined }
          : message)
        : [...state.messages, optimisticMessage],
    }));
    const progressPollGeneration =
      get().startComposerProgressPolling(activeSessionId);
    const inflightPollGeneration =
      get().startInflightMessagesPolling(activeSessionId);

    try {
      const result = await api.sendMessage(activeSessionId, optimisticMessage.content, clientRequestId, stateId, signal);
      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      // Sync the chat panel against the durable DB state before applying the
      // POST's metadata. After this await, get().messages contains every
      // assistant row the compose loop persisted (and the canonical user row
      // — the optimistic local-* version is gone). The set() block below
      // then only needs to update derived state (compositionState, proposals,
      // isComposing) without re-appending the final assistant message that
      // the poll has already pulled in.
      await get().loadInflightMessages(activeSessionId, inflightPollGeneration);
      // Navigation or a newer turn can happen during this sync. The older
      // response must not publish metadata over the newer turn's state.
      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      const { message, state } = result;
      const proposals = result.proposals ?? [];
      set((s) => {
        const previousVersion = s.compositionState?.version ?? null;
        const newVersion = state?.version ?? null;
        const versionChanged =
          newVersion !== null && newVersion !== previousVersion;

        // R4-H3: Clear validation BEFORE updating compositionState
        // when a new state version arrives from the composer
        if (versionChanged) {
          getExecutionStore().clearValidation();
        }

        // Clear selection if the selected node no longer exists in new state
        const newState = state ?? s.compositionState;
        const nodeStillExists =
          !s.selectedNodeId ||
          newState?.nodes.some((n) => n.id === s.selectedNodeId);

        // After loadInflightMessages the message list reflects the canonical
        // DB state — the optimistic local-* row has been dropped and every
        // assistant row the compose loop persisted (including the final
        // ``message`` the POST returned) is present. We still defensively
        // backfill in case the poll request failed: clear the optimistic's
        // pending status and append the final message only when neither is
        // already represented in s.messages (dedup by id).
        const seen = new Set(s.messages.map((m) => m.id));
        const repaired = s.messages.map((existing) =>
          existing.id === optimisticMessage.id
            ? {
                ...existing,
                local_status: undefined,
                local_error: undefined,
                local_failure_code: undefined,
              }
            : existing,
        );
        const finalMessages = seen.has(message.id)
          ? repaired
          : repaired.concat(message);

        return {
          messages: finalMessages,
          compositionState: newState,
          // Persist the mutation verdict for the terminal completion badge
          // (elspeth-bf9c296ee5): "Pipeline updated" vs "Response ready" is
          // this comparison, not the generic terminal phase.
          lastComposeChangedPipeline: versionChanged,
          compositionProposals: mergeCompositionProposals(
            s.compositionProposals,
            proposals,
          ),
          isComposing: false,
          ...(nodeStillExists ? {} : { selectedNodeId: null }),
        };
      });

      // Fire-and-forget: refresh blob list in case the LLM created files
      useBlobStore.getState().loadBlobs(activeSessionId);
      // Fire-and-forget: refresh the session list. The backend send_message
      // route may have auto-titled this session (first-message-of-session
      // generates a 3-6 word title via a side LLM call and writes it
      // before send_message returns). Refreshing keeps the session switcher
      // title in step with the DB without a manual reload.
      void get().loadSessions();
      void refreshInterpretationEventsForSession(activeSessionId);
    } catch (err) {
      const acceptedError = err as ApiError;
      if (
        acceptedError.status === 409 &&
        acceptedError.error_type === "message_already_accepted" &&
        acceptedError.client_request_id === clientRequestId &&
        typeof acceptedError.user_message_id === "string"
      ) {
        if (freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
          set((state) => ({
            isComposing: false,
            messages: state.messages.map((message) =>
              message.id === optimisticMessage.id ||
              (message.role === "user" && message.client_request_id === clientRequestId)
              ? { ...message, local_accepted_user_message_id: acceptedError.user_message_id }
              : message),
          }));
          await reconcileAcceptedSend(
            activeSessionId,
            optimisticMessage.id,
            clientRequestId,
            acceptedError.user_message_id,
            inflightPollGeneration,
            progressPollGeneration,
          );
        }
        return;
      }
      let errorMessage: string;
      // Client-side abort (the useComposer COMPOSE_TIMEOUT_MS guard or any
      // user-supplied signal) rejects with the raw abort-reason string, or a
      // DOMException named 'AbortError' when aborted without a reason —
      // never a structured ApiError. The apiErr.detail fallback below would
      // otherwise mask it as a generic send failure.
      if (isComposeAbort(err)) {
        errorMessage = composeAbortMessage(signal);
      } else {
        const apiErr = err as ApiError;
        // Error dispatch based on HTTP status + error_type field
        if (apiErr.status === 422 && apiErr.error_type === "convergence") {
          errorMessage = formatConvergenceError(apiErr);
        } else if (
          apiErr.status === 502 &&
          apiErr.error_type === "llm_unavailable"
        ) {
          errorMessage = formatLlmUnavailableError(apiErr);
        } else if (
          apiErr.status === 502 &&
          apiErr.error_type === "llm_auth_error"
        ) {
          errorMessage = formatLlmAuthError(apiErr);
        } else if (apiErr.error_type === "audit_integrity_error") {
          errorMessage = formatAuditIntegrityError(apiErr);
        } else {
          errorMessage =
            apiErr.detail ?? "Failed to send message. Please try again.";
        }
      }
      const apiErr = err as ApiError;
      // F-4b: on an audit-integrity refusal the user row IS committed (the
      // insert precedes every audit-guard raise site) — marking it failed is
      // the lie the user acts on (re-sending a duplicate). Clear the pending
      // bit instead: saved, no reply.
      const auditIntegrityRefusal =
        !isComposeAbort(err) && apiErr.error_type === "audit_integrity_error";
      const localFailureCode =
        !isComposeAbort(err) && apiErr.error_type === "message_idempotency_conflict"
          ? "message_idempotency_conflict"
          : !isComposeAbort(err) && typeof apiErr.failure_code === "string"
            ? apiErr.failure_code
            : undefined;
      const recoveryPatch = isComposerRecoveryError(apiErr)
        ? {
            recoveryError: apiErr,
            recoveryStartedCompositionVersion,
          }
        : {};
      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      // Applied AFTER recoveryPatch below so a salvaged draft rebaselines the
      // apply-confirmation gate onto the version the store now shows.
      const partialStatePatch = isComposeAbort(err)
        ? {}
        : convergencePartialStatePatch(apiErr, get().selectedNodeId);
      set((state) => ({
        isComposing: false,
        error: errorMessage,
        messages: state.messages.map((existing) =>
          existing.id === optimisticMessage.id ||
          (existing.role === "user" && existing.client_request_id === clientRequestId)
            ? auditIntegrityRefusal
              ? {
                  ...existing,
                  local_status: undefined,
                  local_error: undefined,
                  local_failure_code: undefined,
                }
              : {
                  ...existing,
                  local_status: "failed",
                  local_error: errorMessage,
                  local_failure_code: localFailureCode,
                }
            : existing,
        ),
        ...recoveryPatch,
        ...partialStatePatch,
      }));
      if (isComposeAbort(err)) {
        // The turn ran (and was cancelled) server-side; pull its durable
        // partial results into view (see resyncAfterAbortedComposeTurn).
        await resyncAfterAbortedComposeTurn(
          activeSessionId,
          progressPollGeneration,
          inflightPollGeneration,
          recoveryStartedCompositionVersion,
        );
      } else if (isAmbiguousComposeNetworkFailure(err)) {
        await resyncAfterAmbiguousComposeFailure(
          activeSessionId,
          progressPollGeneration,
          inflightPollGeneration,
          recoveryStartedCompositionVersion,
          baselineMessageIds,
          optimisticMessage.id,
          content,
          clientRequestId,
        );
      }
    } finally {
      get().stopInflightMessagesPolling(activeSessionId, inflightPollGeneration);
      get().stopComposerProgressPolling(activeSessionId, progressPollGeneration);
      // One-shot terminal pickup — only while this turn still owns the
      // poller; a newer turn's own polling handles it otherwise. The
      // generation goes THROUGH the read as well, because a newer turn can
      // claim the poller during its await.
      if (progressPollGeneration === composerProgressPollGeneration) {
        await get().loadComposerProgress(activeSessionId, {
          ownerGeneration: progressPollGeneration,
        });
      }
    }
  },

  async loadCompositionProposals(sessionId?: string) {
    const targetSessionId = sessionId ?? get().activeSessionId;
    if (!targetSessionId || targetSessionId !== get().activeSessionId) return;
    const generation = sessionPublicationGeneration;
    const isCurrent = () => get().activeSessionId === targetSessionId && sessionPublicationGeneration === generation;

    const snapshot = beginProposalSnapshot();
    try {
      const proposals = await api.fetchCompositionProposals(targetSessionId);
      if (!isCurrent()) {
        return;
      }
      set((state) => ({ compositionProposals: snapshot.reconcile(state.compositionProposals, proposals ?? []) }));
    } catch {
      if (isCurrent() && snapshot.isCurrent()) set({ error: "Failed to load composition proposals. Please try again." });
    }
  },

  async acceptProposal(proposalId: string) {
    const { activeSessionId } = get();
    const publicationGeneration = sessionPublicationGeneration;
    const isCurrent = () =>
      get().activeSessionId === activeSessionId &&
      sessionPublicationGeneration === publicationGeneration;
    if (!activeSessionId) {
      throw new Error("acceptProposal called without active session");
    }

    set((state) => ({
      error: null,
      proposalActionPendingIds: Array.from(
        new Set([...state.proposalActionPendingIds, proposalId]),
      ),
    }));

    try {
      const proposal = await api.acceptCompositionProposal(
        activeSessionId,
        proposalId,
        get().compositionProposals.find((item) => item.id === proposalId)
          ?.pipeline_metadata?.draft_hash ?? null,
      );
      if (!isCurrent()) return;
      acceptedProposalDecisionSequence += 1;
      getExecutionStore().clearValidation();
      // The write receipt remains authoritative even if read-model hydration fails.
      // Do not expose the pre-accept pipeline as the accepted composition.
      set((state) => ({
        compositionState: null,
        compositionStateLoaded: false,
        compositionProposals: mergeCompositionProposals(state.compositionProposals, [proposal]),
      }));
      let compositionState: CompositionState | null;
      let proposals: CompositionProposal[];
      const snapshot = beginProposalSnapshot();
      try {
        [compositionState, proposals] = await Promise.all([
          api.fetchCompositionState(activeSessionId),
          api.fetchCompositionProposals(activeSessionId),
        ]);
      } catch {
        if (isCurrent()) {
          set({ error: "Proposal accepted, but the updated pipeline could not be loaded. Reload the session to refresh it." });
        }
        return;
      }
      if (!isCurrent()) return;
      set((state) => ({
        compositionState: state.compositionState !== null && (
          compositionState === null || state.compositionState.version > compositionState.version
        ) ? state.compositionState : compositionState,
        compositionStateLoaded: true,
        compositionProposals: mergeCompositionProposals(
          snapshot.reconcile(state.compositionProposals, proposals ?? []),
          [proposal],
        ),
      }));
      void refreshInterpretationEventsForSession(activeSessionId);
    } catch (err) {
      if (isHttpConflict(err)) {
        await reconcileProposalConflict(activeSessionId, proposalId, err as ApiError, isCurrent);
      } else {
        const apiErr = err as ApiError;
        // proposal_validation_failed (HTTP 422) carries structured
        // validation entries plus a server-side auto-reject side effect.
        // Reload proposals so the now-rejected one drops off the pending
        // banner; surface the entries as bullet points in the error
        // banner via `errorDetails`. Without this, the toast renders the
        // Pydantic-flattened message as one wall-of-text line and the
        // banner keeps showing the proposal as actionable until refresh.
        const isProposalValidationFailure =
          apiErr.error_type === "proposal_validation_failed";
        if (isProposalValidationFailure) {
          await get().loadCompositionProposals(activeSessionId);
        }
        if (isCurrent()) {
          const validationEntries = apiErr.validation_errors as
            | Array<{ message?: string }>
            | undefined;
          const errorDetails =
            isProposalValidationFailure && Array.isArray(validationEntries)
              ? validationEntries
                  .map((entry) => entry?.message)
                  .filter((msg): msg is string => typeof msg === "string" && msg.length > 0)
              : null;
          set({
            error: apiErr.detail ?? "Failed to accept proposal. Please try again.",
            errorDetails: errorDetails && errorDetails.length > 0 ? errorDetails : null,
          });
        }
      }
    } finally {
      if (isCurrent()) {
        set((state) => ({
          proposalActionPendingIds: state.proposalActionPendingIds.filter(
            (id) => id !== proposalId,
          ),
        }));
      }
    }
  },

  async rejectProposal(proposalId: string) {
    const { activeSessionId } = get();
    const publicationGeneration = sessionPublicationGeneration;
    const isCurrent = () =>
      get().activeSessionId === activeSessionId &&
      sessionPublicationGeneration === publicationGeneration;
    if (!activeSessionId) {
      throw new Error("rejectProposal called without active session");
    }

    set((state) => ({
      error: null,
      proposalActionPendingIds: Array.from(
        new Set([...state.proposalActionPendingIds, proposalId]),
      ),
    }));

    try {
      const proposal = await api.rejectCompositionProposal(
        activeSessionId,
        proposalId,
      );
      if (!isCurrent()) return;
      set((state) => ({
        compositionProposals: mergeCompositionProposals(state.compositionProposals, [proposal]),
      }));
      let proposals: CompositionProposal[];
      const snapshot = beginProposalSnapshot();
      try {
        proposals = await api.fetchCompositionProposals(activeSessionId);
      } catch {
        if (isCurrent()) {
          set({ error: "Proposal rejected, but the proposal list could not be refreshed. Reload the session to refresh it." });
        }
        return;
      }
      if (!isCurrent()) {
        return;
      }
      set((state) => ({
        compositionProposals: mergeCompositionProposals(
          snapshot.reconcile(state.compositionProposals, proposals ?? []),
          [proposal],
        ),
      }));
    } catch (err) {
      if (isHttpConflict(err)) {
        await reconcileProposalConflict(activeSessionId, proposalId, err as ApiError, isCurrent);
      } else if (isCurrent()) {
        const apiErr = err as ApiError;
        set({
          error: apiErr.detail ?? "Failed to reject proposal. Please try again.",
        });
      }
    } finally {
      if (isCurrent()) {
        set((state) => ({
          proposalActionPendingIds: state.proposalActionPendingIds.filter(
            (id) => id !== proposalId,
          ),
        }));
      }
    }
  },

  async loadComposerProgress(
    sessionId?: string,
    options?: { discardStaleTerminal?: boolean; ownerGeneration?: number },
  ) {
    const targetSessionId = sessionId ?? get().activeSessionId;
    if (!targetSessionId) return;

    // Ownership fence (polling audit 2026-09-22, finding 2), two modes, the
    // same shape loadInflightMessages already uses.
    //
    // Interval tick (ownerGeneration omitted): the poller claim is captured
    // BEFORE the fetch and the reply applies only while that same claim is
    // still live for this session. Stopped, rebound to another session, or
    // reclaimed by a newer same-session turn all drop it — otherwise a tick
    // started mid-turn lands after the turn's own final read and rolls the
    // finished turn back to a non-terminal phase, with no poller left to
    // repair it.
    //
    // Owning turn's explicit read (ownerGeneration = what its
    // startComposerProgressPolling returned): it deliberately runs after the
    // stop, so the poller being stopped must NOT drop it. Only the session
    // changing, or a newer turn claiming the poller, does.
    const pollGeneration = composerProgressPollGeneration;
    const readTicket = ++composerProgressReadTicket;
    try {
      const progress = await api.fetchComposerProgress(targetSessionId);
      const current = get();
      if (current.activeSessionId !== targetSessionId) {
        return;
      }
      if (options?.ownerGeneration === undefined) {
        if (
          composerProgressPollSessionId !== targetSessionId ||
          composerProgressPollGeneration !== pollGeneration
        ) {
          return;
        }
      } else if (composerProgressPollGeneration !== options.ownerGeneration) {
        return;
      }
      // Ordering, once ownership holds: a reply older than one already
      // applied is stale by arrival, not by owner. Claimed before the
      // content decision below so a discarded stale-terminal reply still
      // retires the tickets beneath it.
      if (readTicket <= composerProgressAppliedTicket) {
        return;
      }
      composerProgressAppliedTicket = readTicket;
      const isTerminal = TERMINAL_COMPOSER_PROGRESS_PHASES.has(progress.phase);
      if (options?.discardStaleTerminal && isTerminal && !composerProgressPollSeenNonTerminal) {
        return;
      }
      if (progress.phase !== "idle" && !isTerminal) {
        composerProgressPollSeenNonTerminal = true;
      }
      set({ composerProgress: progress.phase === "idle" ? null : progress });
    } catch {
      // Composer progress is advisory. Keep the local heuristic fallback.
    }
  },

  startComposerProgressPolling(sessionId: string) {
    clearComposerProgressPollTimer();
    composerProgressPollGeneration += 1;
    composerProgressPollSessionId = sessionId;
    composerProgressPollSeenNonTerminal = false;
    set({ composerProgress: null });
    void get().loadComposerProgress(sessionId, { discardStaleTerminal: true });
    composerProgressPollTimer = setInterval(() => {
      if (composerProgressPollSessionId !== sessionId) return;
      void useSessionStore
        .getState()
        .loadComposerProgress(sessionId, { discardStaleTerminal: true });
    }, COMPOSER_PROGRESS_POLL_INTERVAL_MS);
    return composerProgressPollGeneration;
  },

  stopComposerProgressPolling(sessionId?: string, generation?: number) {
    if (generation !== undefined && generation !== composerProgressPollGeneration) {
      // A newer turn claimed the poller after this caller's start — the
      // teardown belongs to that turn now.
      return;
    }
    if (
      sessionId !== undefined &&
      composerProgressPollSessionId !== null &&
      composerProgressPollSessionId !== sessionId
    ) {
      return;
    }
    clearComposerProgressPollTimer();
  },

  async loadInflightMessages(
    sessionId: string,
    ownerGeneration?: number,
  ): Promise<ChatMessage[] | null> {
    // Refresh the chat messages from the server so newly-persisted assistant
    // rows from the inflight compose loop are visible immediately. The
    // optimistic local-* user message is preserved when its canonical
    // counterpart hasn't appeared in the fresh list yet (race between the
    // first poll and the route's user-message persist); once the canonical
    // user row arrives, the optimistic one is dropped.
    //
    // Ownership fence (elspeth-90f453d7b2) has two modes.
    //
    // Interval tick (ownerGeneration omitted): the poll generation is
    // captured BEFORE the fetch and the response is applied only while the
    // same poller claim is still live for this session. Stopped (id null),
    // rebound to another session, or reclaimed by a newer same-session turn
    // (generation moved) all drop it — otherwise a tick started mid-compose
    // that lands after the turn settled replaces the post-settle list with
    // an older one and wipes the final reply, with no poller left to repair
    // it.
    //
    // Owning turn's explicit sync (ownerGeneration = the generation its
    // startInflightMessagesPolling returned; sendMessage/retryMessage
    // post-settle sync and the aborted-turn resync): the poller being
    // stopped is NOT a reason to drop it. selectSession stops the poller on
    // A -> B -> A without aborting the turn, and this sync is then the only
    // thing that brings the rows persisted during the turn into view. It is
    // dropped only when the session is no longer active or a newer turn on
    // the SAME session has claimed the poller (that turn owns the sync now).
    const pollGeneration = inflightMessagesPollGeneration;
    const readTicket = ++inflightMessagesReadTicket;
    try {
      const fresh = await api.fetchMessages(sessionId);
      if (get().activeSessionId !== sessionId) return null;
      if (ownerGeneration === undefined) {
        if (
          inflightMessagesPollSessionId !== sessionId ||
          inflightMessagesPollGeneration !== pollGeneration
        ) {
          return null;
        }
      } else if (
        inflightMessagesLatestClaimBySession.get(sessionId) !== ownerGeneration
      ) {
        return null;
      }
      // Ordering fence (finding 3). Both modes take a ticket: the owning
      // turn's sync runs while the poller is still live, so a tick of the
      // SAME generation can answer after it and pass every check above while
      // carrying the list from before the final reply was persisted.
      if (readTicket <= inflightMessagesAppliedTicket) {
        return null;
      }
      inflightMessagesAppliedTicket = readTicket;
      set((s) => {
        if (s.activeSessionId !== sessionId) return s;
        const localOptimistic = s.messages.filter((m) =>
          m.id.startsWith("local-"),
        );
        const survivors = localOptimistic.filter(
          (local) =>
            !fresh.some(
              (f) => f.role === "user" &&
                local.client_request_id != null &&
                f.client_request_id === local.client_request_id,
            ),
        );
        const reconciled = fresh.map((message, index) => {
          if (message.role !== "user" || !message.client_request_id) return message;
          const prior = s.messages.find((existing) =>
            existing.role === "user" &&
            existing.client_request_id === message.client_request_id
          );
          if (!prior) return message;
          const hasReply = hasGenuineReplyForUser(fresh, index);
          return {
            ...message,
            local_requested_state_id: prior.local_requested_state_id,
            local_accepted_user_message_id: prior.local_accepted_user_message_id,
            ...(!hasReply && prior.local_status === "failed"
              ? {
                  local_status: "failed" as const,
                  local_error: prior.local_error,
                  local_failure_code: prior.local_failure_code,
                }
              : {}),
          };
        });
        return { messages: [...reconciled, ...survivors] };
      });
      return fresh;
    } catch {
      // Inflight polling is advisory — failures keep the existing UI state
      // until the next poll or the POST completion handler refreshes it.
      return null;
    }
  },

  startInflightMessagesPolling(sessionId: string) {
    clearInflightMessagesPollTimer();
    inflightMessagesPollGeneration += 1;
    inflightMessagesPollSessionId = sessionId;
    inflightMessagesLatestClaimBySession.set(
      sessionId,
      inflightMessagesPollGeneration,
    );
    inflightMessagesPollTimer = setInterval(() => {
      if (inflightMessagesPollSessionId !== sessionId) return;
      void useSessionStore.getState().loadInflightMessages(sessionId);
    }, INFLIGHT_MESSAGES_POLL_INTERVAL_MS);
    return inflightMessagesPollGeneration;
  },

  stopInflightMessagesPolling(sessionId?: string, generation?: number) {
    if (generation !== undefined && generation !== inflightMessagesPollGeneration) {
      // A newer turn claimed the poller after this caller's start — the
      // teardown belongs to that turn now.
      return;
    }
    if (
      sessionId !== undefined &&
      inflightMessagesPollSessionId !== null &&
      inflightMessagesPollSessionId !== sessionId
    ) {
      return;
    }
    clearInflightMessagesPollTimer();
  },

  async retryMessage(messageId: string, signal?: AbortSignal) {
    const { activeSessionId, messages, isComposing } = get();
    if (!activeSessionId) return;
    // Same synchronous admission gate as sendMessage (elspeth-3f38ebb1b5).
    if (isComposing) return;
    const recoveryStartedCompositionVersion =
      get().compositionState?.version ?? null;

    const message = messages.find((entry) => entry.id === messageId);
    if (!message || message.role !== "user") return;
    if (message.client_request_id && message.local_accepted_user_message_id) {
      set({ error: null });
      const progressGeneration = get().startComposerProgressPolling(activeSessionId);
      const inflightGeneration = get().startInflightMessagesPolling(activeSessionId);
      try {
        await reconcileAcceptedSend(
          activeSessionId, message.id, message.client_request_id,
          message.local_accepted_user_message_id, inflightGeneration, progressGeneration,
        );
      } finally {
        get().stopInflightMessagesPolling(activeSessionId, inflightGeneration);
        get().stopComposerProgressPolling(activeSessionId, progressGeneration);
      }
      return;
    }
    if (message.id.startsWith("local-")) {
      if (!message.client_request_id) return;
      await get().sendMessage(message.content, signal, message.id);
      return;
    }
    const baselineMessageIds = new Set(messages.map((entry) => entry.id));

    set((state) => ({
      isComposing: true,
      error: null,
      composerProgress: null,
      // Same unknown-while-in-flight contract as sendMessage.
      lastComposeChangedPipeline: null,
      messages: state.messages.map((existing) =>
        existing.id === messageId
          ? { ...existing, local_status: "pending" }
          : existing,
      ),
    }));
    const progressPollGeneration =
      get().startComposerProgressPolling(activeSessionId);
    const inflightPollGeneration =
      get().startInflightMessagesPolling(activeSessionId);

    try {
      // Use recompose (not sendMessage) — the user message is already
      // persisted from the original send. Calling sendMessage again
      // would insert a duplicate user message.
      const result = await api.recompose(activeSessionId, messageId, signal);
      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      // Sync the chat panel against the DB state (see sendMessage for
      // rationale).
      await get().loadInflightMessages(activeSessionId, inflightPollGeneration);
      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      const { message: assistantMessage, state } = result;
      const proposals = result.proposals ?? [];
      set((s) => {
        const previousVersion = s.compositionState?.version ?? null;
        const newVersion = state?.version ?? null;
        const versionChanged =
          newVersion !== null && newVersion !== previousVersion;

        if (versionChanged) {
          getExecutionStore().clearValidation();
        }

        // Clear selection if the selected node no longer exists in new state
        const newState = state ?? s.compositionState;
        const nodeStillExists =
          !s.selectedNodeId ||
          newState?.nodes.some((n) => n.id === s.selectedNodeId);

        // Polling has loaded the canonical messages list. Defensive backfill
        // mirrors the sendMessage success branch: clear the retried message's
        // pending status, and only append the recomposed assistant message
        // if it isn't already represented (dedup by id).
        const seen = new Set(s.messages.map((m) => m.id));
        const repaired = s.messages.map((existing) =>
          existing.id === messageId
            ? {
                ...existing,
                local_status: undefined,
                local_error: undefined,
                local_failure_code: undefined,
              }
            : existing,
        );
        const finalMessages = seen.has(assistantMessage.id)
          ? repaired
          : repaired.concat(assistantMessage);

        return {
          messages: finalMessages,
          compositionState: newState,
          // Mirror of the sendMessage success branch (elspeth-bf9c296ee5).
          lastComposeChangedPipeline: versionChanged,
          compositionProposals: mergeCompositionProposals(
            s.compositionProposals,
            proposals,
          ),
          isComposing: false,
          ...(nodeStillExists ? {} : { selectedNodeId: null }),
        };
      });

      // Fire-and-forget: refresh blob list in case the LLM created files
      useBlobStore.getState().loadBlobs(activeSessionId);
      void refreshInterpretationEventsForSession(activeSessionId);
    } catch (err) {
      let errorMessage: string;
      if (isComposeAbort(err)) {
        errorMessage = composeAbortMessage(signal);
      } else {
        const apiErr = err as ApiError;
        errorMessage =
          apiErr.status === 502 && apiErr.error_type === "llm_unavailable"
            ? formatLlmUnavailableError(apiErr)
            : apiErr.status === 502 && apiErr.error_type === "llm_auth_error"
              ? formatLlmAuthError(apiErr)
              : apiErr.status === 422 && apiErr.error_type === "convergence"
                ? formatConvergenceError(apiErr)
                : apiErr.detail ?? "Failed to send message. Please try again.";
      }
      const apiErr = err as ApiError;
      // S1: mirror the sendMessage catch handler — a retry that itself fails
      // with a permanent code ("policy_blocked") must not re-render the
      // Retry invitation it just disproved.
      const localFailureCode =
        !isComposeAbort(err) && apiErr.error_type === "recompose_user_message_mismatch"
          ? "recompose_user_message_mismatch"
          : !isComposeAbort(err) && typeof apiErr.failure_code === "string"
            ? apiErr.failure_code
            : undefined;
      const recoveryPatch = isComposerRecoveryError(apiErr)
        ? {
            recoveryError: apiErr,
            recoveryStartedCompositionVersion,
          }
        : {};

      if (!freeformComposeClaimIsCurrent(activeSessionId, inflightPollGeneration)) {
        return;
      }
      // Mirror of the sendMessage catch: the shared 422 handler already
      // persisted the salvaged draft, so both entry points must show it.
      const partialStatePatch = isComposeAbort(err)
        ? {}
        : convergencePartialStatePatch(apiErr, get().selectedNodeId);
      set((state) => ({
        isComposing: false,
        error: errorMessage,
        messages: state.messages.map((existing) =>
          existing.id === messageId
            ? {
                ...existing,
                local_status: "failed",
                local_error: errorMessage,
                local_failure_code: localFailureCode,
              }
            : existing,
        ),
        ...recoveryPatch,
        ...partialStatePatch,
      }));
      if (isComposeAbort(err)) {
        // The recompose turn ran (and was cancelled) server-side; pull its
        // durable partial results into view (see
        // resyncAfterAbortedComposeTurn).
        await resyncAfterAbortedComposeTurn(
          activeSessionId,
          progressPollGeneration,
          inflightPollGeneration,
          recoveryStartedCompositionVersion,
        );
      } else if (isAmbiguousComposeNetworkFailure(err)) {
        await resyncAfterAmbiguousComposeFailure(
          activeSessionId,
          progressPollGeneration,
          inflightPollGeneration,
          recoveryStartedCompositionVersion,
          baselineMessageIds,
          messageId,
          message.content,
        );
      }
    } finally {
      get().stopInflightMessagesPolling(activeSessionId, inflightPollGeneration);
      get().stopComposerProgressPolling(activeSessionId, progressPollGeneration);
      // One-shot terminal pickup — only while this turn still owns the
      // poller; a newer turn's own polling handles it otherwise. The
      // generation goes THROUGH the read as well, because a newer turn can
      // claim the poller during its await.
      if (progressPollGeneration === composerProgressPollGeneration) {
        await get().loadComposerProgress(activeSessionId, {
          ownerGeneration: progressPollGeneration,
        });
      }
    }
  },

  async forkFromMessage(messageId: string, newContent: string) {
    const { activeSessionId } = get();
    if (!activeSessionId) return;

    clearComposerProgressPollTimer();
    clearInflightMessagesPollTimer();
    let acquisition = acquireSessionOperationRetry("session_fork", activeSessionId, [
      messageId,
      newContent,
    ]);
    if (acquisition.status === "conflict") {
      acquisition = await reconcileSessionOperationRetryConflict(acquisition.existing, "session_fork", activeSessionId, [
      messageId,
      newContent,
    ]);
    }
    if (acquisition.status === "conflict") {
      set(sessionOperationRetryConflictState(acquisition.existing.kind));
      return;
    }
    const retry = acquisition.handle;
    let responseReceived = false;
    let childPublished = false;
    set({ isComposing: true, error: null });
    try {
      const result = await api.forkFromMessage(
        activeSessionId,
        retry.operationId,
        messageId,
        newContent,
      );
      responseReceived = true;

      // The POST returns only a durable locator. Publish that exact child into
      // the global list first, using a minimal merge that preserves concurrent
      // list mutations. Hydrate into locals while the parent remains active;
      // switch atomically only after every load-bearing child read succeeds.
      const authoritativeSessions = await api.fetchSessions();
      set((state) => ({
        sessions: mergeAuthoritativeForkChild(
          state.sessions,
          authoritativeSessions,
          result.session_id,
        ),
        sessionsLoaded: true,
      }));
      childPublished = get().sessions.some((session) => session.id === result.session_id);
      if (!childPublished) {
        throw new Error("Fork result was not published to the session list");
      }
      if (get().activeSessionId !== activeSessionId) {
        clearSessionOperationRetry(retry);
        return;
      }

      const [messages, compositionState, compositionProposals, composerPreferences] =
        await Promise.all([
          api.fetchMessages(result.session_id),
          api.fetchCompositionState(result.session_id),
          api.fetchCompositionProposals(result.session_id),
          api.fetchComposerPreferences(result.session_id),
        ]);
      if (!get().sessions.some((session) => session.id === result.session_id)) {
        throw new Error("Published fork result disappeared during hydration");
      }
      if (get().activeSessionId !== activeSessionId) {
        clearSessionOperationRetry(retry);
        return;
      }
      useBlobStore.getState().activateSession(result.session_id);
      let activatedChild = false;
      set((state) => {
        if (state.activeSessionId !== activeSessionId) {
          return {};
        }
        activatedChild = true;
        advanceSessionPublicationGeneration();
        getExecutionStore().clearValidation();
        return {
          activeSessionId: result.session_id,
          messages,
          compositionState,
          compositionStateLoaded: true,
          compositionProposals: compositionProposals ?? [],
          composerPreferences: composerPreferences ?? null,
          staleProposalIds: [],
          proposalActionPendingIds: [],
          composerProgress: null,
          stateVersions: [],
          isComposing: false,
          error: null,
          selectedNodeId: null,
          ...clearedRecoveryState(),
        };
      });
      clearSessionOperationRetry(retry);
      if (!activatedChild) {
        return;
      }
      useBlobStore.getState().loadBlobs(result.session_id);
      void useInterpretationEventsStore.getState().refreshAll(result.session_id);
    } catch (err) {
      if (childPublished && get().activeSessionId !== activeSessionId) {
        clearSessionOperationRetry(retry);
      }
      if (
        !responseReceived &&
        !api.isForkCommittedResponseError(err) &&
        !isAmbiguousSessionOperationRetryFailure(err)
      ) {
        clearSessionOperationRetry(retry);
      }
      if (get().activeSessionId === activeSessionId) {
        set({
          isComposing: false,
          composerProgress: null,
          error: "Failed to fork conversation. Please try again.",
        });
      }
    }
  },

  openRecoveryFromError(error, recoveryStartedCompositionVersion) {
    if (!isComposerRecoveryError(error)) {
      return false;
    }
    set({
      recoveryError: error,
      recoveryStartedCompositionVersion,
    });
    return true;
  },

  applyRecoveredState(options) {
    const { recoveryError, recoveryStartedCompositionVersion, compositionState } =
      get();
    if (recoveryError === null) {
      return { applied: false, needsConfirmation: false };
    }

    const currentVersion = compositionState?.version ?? null;
    if (
      options?.confirmed !== true &&
      currentVersion !== recoveryStartedCompositionVersion
    ) {
      return { applied: false, needsConfirmation: true };
    }

    const recoveredState = recoveryError.partial_state;
    if (
      recoveryError.partial_state_save_failed === true ||
      typeof recoveredState.id !== "string" ||
      recoveredState.id.trim() === ""
    ) {
      set({
        error:
          "Recovered draft was not saved on the server. Discard recovery and retry the composer step.",
      });
      return { applied: false, needsConfirmation: false };
    }
    getExecutionStore().clearValidation();
    set((state) => {
      const nodeStillExists =
        !state.selectedNodeId ||
        recoveredState.nodes.some((node) => node.id === state.selectedNodeId);
      return {
        compositionState: recoveredState,
        ...(nodeStillExists ? {} : { selectedNodeId: null }),
        ...clearedRecoveryState(),
      };
    });
    return { applied: true, needsConfirmation: false };
  },

  discardRecovery() {
    set(clearedRecoveryState());
  },











  async loadStateVersions() {
    const { activeSessionId } = get();
    if (!activeSessionId) return;

    set({ isLoadingVersions: true });
    try {
      const versions = await api.fetchStateVersions(activeSessionId);
      set({ stateVersions: versions, isLoadingVersions: false });
    } catch {
      // Version history is non-critical -- fail silently
      set({ isLoadingVersions: false });
    }
  },

  async revertToVersion(stateId: string) {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    let acquisition = acquireSessionOperationRetry("state_revert", activeSessionId, [stateId]);
    if (acquisition.status === "conflict") {
      acquisition = await reconcileSessionOperationRetryConflict(acquisition.existing, "state_revert", activeSessionId, [stateId]);
    }
    if (acquisition.status === "conflict") {
      set(sessionOperationRetryConflictState(acquisition.existing.kind));
      return;
    }
    const retry = acquisition.handle;

    try {
      // R4-H3: Clear validation BEFORE updating compositionState
      // to prevent a frame where stale validation is visible with the new version
      getExecutionStore().clearValidation();

      const compositionState = await api.revertToVersion(
        activeSessionId,
        stateId,
        retry.operationId,
      );
      // Drop a result that settles after the user switches sessions.
      if (get().activeSessionId !== activeSessionId) {
        clearSessionOperationRetry(retry);
        return;
      }
      // Clear selection — the reverted version may not contain the selected node
      set({
        compositionState,
        selectedNodeId: null,
      });
      // Revert is a state-producing route: restoring an older pending
      // interpretation requirement can mint fresh backend review events.
      // Pull them into the independent event store so execution does not stay
      // blocked behind an invisible card until a full session reload.
      void refreshInterpretationEventsForSession(activeSessionId);
      clearSessionOperationRetry(retry);
    } catch (err) {
      if (!isAmbiguousSessionOperationRetryFailure(err)) {
        clearSessionOperationRetry(retry);
      }
      set({ error: "Failed to revert to version. Please try again." });
    }
  },

  applyResolvedInterpretation(newState: CompositionState | null) {
    const { activeSessionId } = get();
    if (!activeSessionId) return;

    // Display sync: resolving an interpretation patches the pipeline server-side
    // (bakes the chosen prompt template / model / decision) and returns the new
    // composition. Reflect it so the rendered pipeline matches what will run.
    if (newState !== null) {
      set((s) => ({
        compositionState: newState,
        selectedNodeId:
          !s.selectedNodeId ||
          newState.nodes.some((n) => n.id === s.selectedNodeId)
            ? s.selectedNodeId
            : null,
      }));
    }

    // Gate clearing: re-validate explicitly. The run-gate (ExecuteButton) and
    // the validation system message are driven by validationResult, which goes
    // stale after a resolve — the auto-validate subscription only fires on a
    // composition-version bump, which a resolve does not guarantee (and
    // newState may be null). Without this the user resolves every review and
    // the Run button stays disabled with no signal of what changed. validate()
    // re-checks server-side pending interpretations, so once the last review is
    // resolved the gate opens.
    void getExecutionStore().validate(activeSessionId);
  },

  clearError() {
    set({
      error: null,
      errorDetails: null,
    });
  },

  selectNode(nodeId: string | null) {
    set({ selectedNodeId: nodeId });
  },

  consumeAuthoringFocusRequest() {
    set({ authoringFocusRequested: false });
  },

  injectSystemMessage(content: string, stableId?: string) {
    const { activeSessionId } = get();
    if (!activeSessionId) return;

    const messageId = stableId ?? `system-${crypto.randomUUID()}`;

    const systemMessage: ChatMessage = {
      id: messageId,
      session_id: activeSessionId,
      role: "system",
      content,
      tool_calls: null,
      created_at: new Date().toISOString(),
    };

    set((state) => {
      // If a stable ID was provided, replace any existing message with
      // that ID instead of appending. This prevents noise accumulation
      // from repeated validation cycles.
      const filtered = stableId
        ? state.messages.filter((m) => m.id !== stableId)
        : state.messages;
      return { messages: [...filtered, systemMessage] };
    });
  },

  reset() {
    clearComposerProgressPollTimer();
    clearInflightMessagesPollTimer();
    clearAllSessionOperationRetries();
    advanceSessionPublicationGeneration();
    useBlobStore.getState().activateSession(null);
    // composeTimeoutReady resets to false via initialState; App.checkHealth
    // re-latches it on re-authentication. The module ceiling (composeTimeoutMs)
    // is a backend property that harmlessly persists — it is only read while
    // ready, which a fresh checkHealth re-establishes before any send.
    // Fresh array (not initialState.sessions) so loadSessions' reference
    // guard can't mistake a post-reset store for the pre-reset one it
    // captured before a still-in-flight fetch (logout/login ABA).
    set({ ...initialState, sessions: [] });
  },
}));
