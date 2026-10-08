import { redactedCase } from "@/test/redactedArgumentFixture";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import {
  _resetSubscriptionsForTesting,
} from "@/stores/subscriptions";
import { useSessionStore } from "@/stores/sessionStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useInlineSourceStore } from "@/stores/inlineSourceStore";
import { useBlobStore } from "@/stores/blobStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useExecutionStore } from "@/stores/executionStore";
import { resetStore } from "@/test/store-helpers";
import { useComposer } from "@/hooks/useComposer";
import { makeComposition } from "@/test/composerFixtures";
import * as apiClient from "@/api/client";
import type {
  BlobMetadata,
  ChatMessage,
  ComposerProgressSnapshot,
  CompositionProposal,
  Session,
} from "@/types/api";
import { COMPOSE_CONNECTING_MESSAGE, COMPOSE_UNAVAILABLE_MESSAGE } from "@/config/composer";
import type { InterpretationEvent } from "@/types/interpretation";
import { BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX } from "@/types/interpretation";

vi.mock("@/hooks/useComposer", () => ({
  useComposer: vi.fn(),
}));

const inlineRunResultsMountSpy = vi.hoisted(() => vi.fn());

vi.mock("@/components/execution/InlineRunResults", () => ({
  InlineRunResults: () => {
    inlineRunResultsMountSpy();
    return <div data-testid="inline-run-results" />;
  },
}));

// Spy-style mock of the blob-fetch surface so the inline-source-projection
// effect can be driven from the test. The actual module is preserved
// (`...actual`) so we don't accidentally stub other exports the file uses.
vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    getBlobMetadata: vi.fn(),
    previewBlobContent: vi.fn(),
    // ModelChip's data source. Reset (undefined resolution) in most tests →
    // the chip renders nothing; the model-chip tests set a resolved value.
    fetchSystemStatus: vi.fn(),
  };
});

const mockedChatInputUpload = vi.hoisted(() => ({
  blob: null as BlobMetadata | null,
  requests: [] as Array<{
    requestId: string;
    sessionId: string;
    completion: Promise<BlobMetadata>;
  }>,
  completedRequestIds: [] as string[],
  acceptedRequestIds: [] as string[],
  settledRequestIds: [] as string[],
  acceptedFailureRequestIds: [] as string[],
  immediateRequestSeq: 0,
}));

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

vi.mock("./MessageBubble", () => ({
  MessageBubble: ({
    message,
    proposalsByToolCallId,
    staleProposalIds,
    pendingReviewCreatedAt,
  }: {
    message: ChatMessage;
    proposalsByToolCallId?: Map<string, CompositionProposal>;
    staleProposalIds?: string[];
    pendingReviewCreatedAt?: ReadonlyArray<string>;
  }) => {
    const toolCallId = message.tool_calls?.[0]?.id ?? null;
    const proposal = toolCallId
      ? proposalsByToolCallId?.get(toolCallId) ?? null
      : null;
    const isStale = proposal
      ? staleProposalIds?.includes(proposal.id) ?? false
      : false;
    return (
      <div data-testid="message-bubble" data-pending-review-created-at={JSON.stringify(pendingReviewCreatedAt)}>
        <div>{message.content}</div>
        {proposal && <div>{proposal.summary}</div>}
        {isStale && <div>Stale proposal</div>}
      </div>
    );
  },
}));

vi.mock("./ChatInput", async (importOriginal) => ({
  // Pass real non-component exports through (uploadedBlobPromptSentence is
  // consumed by ChatPanel's freeform upload fence).
  ...(await importOriginal<typeof import("./ChatInput")>()),
  ChatInput: ({
    placeholder,
    onSend,
    onCancel,
    disabled,
    maxLength,
    value,
    onChange,
    readOnly,
    onBlobUploaded,
    onBlobUploadStarted,
    onBlobUploadCompleted,
    onBlobUploadRejected,
    onBlobUploadSettled,
    uploadPromptSentence,
    uploadDisabled,
  }: {
    placeholder?: string;
    onSend?: (content: string) => void;
    onCancel?: () => void;
    disabled?: boolean;
    maxLength?: number;
    value?: string;
    onChange?: (value: string) => void;
    readOnly?: boolean;
    onBlobUploaded?: (blob: BlobMetadata) => void;
    onBlobUploadStarted?: (requestId: string, sessionId: string) => void;
    onBlobUploadCompleted?: (
      requestId: string,
      sessionId: string,
      blob: BlobMetadata,
    ) => boolean;
    onBlobUploadRejected?: (requestId: string, sessionId: string) => boolean;
    onBlobUploadSettled?: (requestId: string, sessionId: string) => void;
    uploadPromptSentence?: (filename: string) => string;
    uploadDisabled?: boolean;
  }) => (
    <>
      <button
        type="button"
        data-testid="chat-input"
        data-placeholder={placeholder ?? ""}
        data-disabled={disabled ? "true" : "false"}
        data-has-cancel={onCancel ? "true" : "false"}
        data-max-length={maxLength ?? ""}
        data-value={value ?? ""}
        data-read-only={readOnly ? "true" : "false"}
        onClick={() => {
          onSend?.("test-chat-message");
          onChange?.("");
        }}
      >
        {placeholder ?? ""}
      </button>
      <button
        type="button"
        data-testid="chat-input-type"
        // Simulate the operator typing into the (never-disabled) textarea —
        // used by the prompt-retention tests to prove a restore never
        // clobbers newer typing entered while a send is in flight.
        onClick={() => onChange?.("retyped while pending")}
      >
        simulate typing
      </button>
      <button
        type="button"
        data-testid="chat-input-upload"
        disabled={uploadDisabled}
        onClick={() => {
          const queuedRequest = mockedChatInputUpload.requests.shift();
          if (queuedRequest !== undefined) {
            onBlobUploadStarted?.(queuedRequest.requestId, queuedRequest.sessionId);
            void queuedRequest.completion.then((blob) => {
              useBlobStore.setState((state) => ({
                blobs: [blob, ...state.blobs.filter((item) => item.id !== blob.id)],
              }));
              const accepted =
                onBlobUploadCompleted?.(
                  queuedRequest.requestId,
                  queuedRequest.sessionId,
                  blob,
                ) ?? true;
              if (accepted) {
                mockedChatInputUpload.acceptedRequestIds.push(queuedRequest.requestId);
                onBlobUploaded?.(blob);
                onChange?.(
                  `${value ?? ""}${value ? "\n" : ""}${uploadPromptSentence?.(blob.filename) ?? `I've uploaded "${blob.filename}". Please use it for the role I describe, or ask whether it is a pipeline input, reference table, or LLM prompt.`}`,
                );
              }
              onBlobUploadSettled?.(queuedRequest.requestId, queuedRequest.sessionId);
              mockedChatInputUpload.settledRequestIds.push(queuedRequest.requestId);
              mockedChatInputUpload.completedRequestIds.push(queuedRequest.requestId);
            }, () => {
              const accepted =
                onBlobUploadRejected?.(
                  queuedRequest.requestId,
                  queuedRequest.sessionId,
                ) ?? true;
              if (accepted) {
                mockedChatInputUpload.acceptedFailureRequestIds.push(
                  queuedRequest.requestId,
                );
              }
              onBlobUploadSettled?.(queuedRequest.requestId, queuedRequest.sessionId);
              mockedChatInputUpload.settledRequestIds.push(queuedRequest.requestId);
            });
            return;
          }
          if (mockedChatInputUpload.blob !== null) {
            const blob = mockedChatInputUpload.blob;
            const requestId = `immediate-${++mockedChatInputUpload.immediateRequestSeq}`;
            onBlobUploadStarted?.(requestId, blob.session_id);
            useBlobStore.setState((state) => ({
              blobs: [
                blob,
                ...state.blobs.filter((item) => item.id !== blob.id),
              ],
            }));
            const accepted =
              onBlobUploadCompleted?.(requestId, blob.session_id, blob) ?? true;
            if (accepted) onBlobUploaded?.(blob);
            onBlobUploadSettled?.(requestId, blob.session_id);
          }
        }}
      >
        simulate upload
      </button>
    </>
  ),
}));

vi.mock("@/components/blobs/BlobManager", () => ({
  BlobManager: () => <div data-testid="blob-manager" />,
}));

describe("ChatPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: true,
      compositionState: null,
      error: null,
    });
  });

  it("observes the InlineRunResults sentinel when the retired owner is imported", async () => {
    const { InlineRunResults } = await import(
      "@/components/execution/InlineRunResults"
    );
    render(<InlineRunResults />);

    expect(inlineRunResultsMountSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("inline-run-results")).toBeInTheDocument();
  });

  it("does not mount run results in the freeform authoring pane", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [],
    });

    render(<ChatPanel />);

    expect(screen.queryByTestId("inline-run-results")).toBeNull();
    expect(inlineRunResultsMountSpy).not.toHaveBeenCalled();
  });

  it("passes backend composer progress to the composing indicator", () => {
    const session: Session = {
      id: "session-1",
      title: "Composer session",
      created_at: "2026-04-26T10:00:00Z",
      updated_at: "2026-04-26T10:00:00Z",
    };
    const userMessage: ChatMessage = {
      id: "message-1",
      session_id: "session-1",
      role: "user",
      content: "Exploit this HTML into JSON",
      tool_calls: null,
      created_at: "2026-04-26T10:00:01Z",
      local_status: "pending",
    };
    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "using_tools",
      headline: "The model requested plugin schemas.",
      evidence: ["Checking available source, transform, and sink tools."],
      likely_next: "ELSPETH will use the schemas to choose a pipeline shape.",
      reason: null,
      updated_at: "2026-04-26T10:00:02Z",
    };

    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [userMessage],
      composerProgress: progress,
    });

    render(<ChatPanel />);

    expect(screen.getByText("The model requested plugin schemas.")).toBeInTheDocument();
    expect(
      screen.queryByText("Checking available source, transform, and sink tools."),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("Checking available source, transform, and sink tools.")).toBeInTheDocument();
    expect(screen.queryByText("Working on: convert HTML into JSON")).not.toBeInTheDocument();
  });

  it("keeps terminal composer progress visible after composing ends", () => {
    const session: Session = {
      id: "session-1",
      title: "Composer session",
      created_at: "2026-04-26T10:00:00Z",
      updated_at: "2026-04-26T10:00:00Z",
    };
    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "cancelled",
      headline: "Composition stopped before saving.",
      evidence: ["The request ended before a valid pipeline was saved."],
      likely_next: "Revise the request and send it again.",
      reason: "client_cancelled",
      updated_at: "2026-04-26T10:00:02Z",
    };

    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "Build me a pipeline",
          tool_calls: null,
          created_at: "2026-04-26T10:00:01Z",
        },
      ],
      composerProgress: progress,
    });

    render(<ChatPanel />);

    expect(screen.getByText("Last composer update")).toBeInTheDocument();
    expect(screen.getByText("Composition stopped before saving.")).toBeInTheDocument();
    expect(screen.getByText("Revise the request and send it again.")).toBeInTheDocument();
  });

  it.each(["registered", "unknown"])("offers recovery with the latest %s rejection after canonical messages reload", (latestRejection) => {
    const retryMessage = vi.fn();
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(), retryMessage, isComposing: false, error: null,
    });
    const scaffold = makeComposition(2, { nodes: [] });
    useSessionStore.setState({
      activeSessionId: "session-1",
      composeTimeoutReady: true,
      compositionState: scaffold,
      messages: [{
        id: "cancelled-request", session_id: "session-1", role: "user",
        content: "Assess the case studies", tool_calls: null,
        created_at: "2026-10-01T06:40:19Z",
      }, {
        id: "rejected-proposal", session_id: "session-1", role: "assistant",
        content: "", created_at: "2026-10-01T06:44:12Z",
        tool_calls: [{
          id: "first-rejected-call", type: "function", outcome: "rejected",
          function: { name: "set_pipeline", arguments: "{}" },
          rejection: {
            error_code: "plugin_options_invalid",
            guidance: ["Use the normalized CSV field name."],
          },
        }, {
          id: "rejected-call", type: "function", outcome: "rejected",
          function: { name: "set_pipeline", arguments: "{}" },
          rejection: {
            error_code: "source_data_contract_required",
            guidance: ["Declare an explicit runtime schema or request a source data contract review."],
          },
        }, ...(latestRejection === "unknown" ? [{
          id: "last-unknown-rejection", type: "function", outcome: "rejected" as const,
          function: { name: "set_pipeline", arguments: "{}" },
        }] : [])],
      }],
      composerProgress: {
        session_id: "session-1", request_id: "cancelled-request",
        phase: "cancelled", reason: "client_cancelled",
        headline: "The request was cancelled before the composer finished.",
        evidence: ["The client closed the connection before a response was returned."],
        likely_next: "Retry when ready.", updated_at: "2026-10-01T06:44:18Z",
        inflight_requests: 0,
      },
    });

    render(<ChatPanel />);

    expect(screen.getByText("This request did not finish. The saved pipeline remains a draft for this request.")).toBeInTheDocument();
    if (latestRejection === "registered") {
      expect(screen.getByText("Last validation issue")).toBeInTheDocument();
      expect(screen.getByText("Declare an explicit runtime schema or request a source data contract review.")).toBeInTheDocument();
    } else {
      expect(screen.queryByText("Last validation issue")).not.toBeInTheDocument();
      expect(screen.queryByText("Declare an explicit runtime schema or request a source data contract review.")).not.toBeInTheDocument();
    }
    expect(screen.queryByText("Use the normalized CSV field name.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry interrupted request" }));
    expect(retryMessage).toHaveBeenCalledWith("cancelled-request");
    expect(useSessionStore.getState().compositionState).toBe(scaffold);
  });

  it.each([
    [false, COMPOSE_CONNECTING_MESSAGE],
    [true, COMPOSE_UNAVAILABLE_MESSAGE],
  ])("explains unavailable recovery while timeout readiness is missing (unavailable=%s)", (unavailable, explanation) => {
    const retryMessage = vi.fn();
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(), retryMessage, isComposing: false, error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1", composeTimeoutReady: false,
      composerTimeoutUnavailable: unavailable,
      messages: [{
        id: "cancelled-request", session_id: "session-1", role: "user",
        content: "Assess the case studies", tool_calls: null,
        created_at: "2026-10-01T06:40:19Z",
      }],
      composerProgress: {
        session_id: "session-1", request_id: "cancelled-request",
        phase: "cancelled", reason: "client_cancelled", headline: "Cancelled",
        evidence: [], likely_next: "Retry when ready.",
        updated_at: "2026-10-01T06:44:18Z", inflight_requests: 0,
      },
    });
    render(<ChatPanel />);
    const retry = screen.getByRole("button", { name: "Retry interrupted request" });
    expect(retry).toBeDisabled();
    expect(retry).toHaveAccessibleDescription(explanation);
    expect(screen.getByText(explanation)).toBeInTheDocument();
    fireEvent.click(retry);
    expect(retryMessage).not.toHaveBeenCalled();
  });

  it.each([1, undefined])("withholds cancellation recovery until settlement is confirmed (%s inflight)", (inflight) => {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(), retryMessage: vi.fn(), isComposing: false, error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [{
        id: "cancelled-request", session_id: "session-1", role: "user",
        content: "Assess the case studies", tool_calls: null,
        created_at: "2026-10-01T06:40:19Z",
      }],
      composerProgress: {
        session_id: "session-1", request_id: "cancelled-request",
        phase: "cancelled", reason: "client_cancelled", headline: "Cancelled",
        evidence: [], likely_next: "Retry when ready.",
        updated_at: "2026-10-01T06:44:18Z", inflight_requests: inflight,
      },
    });
    render(<ChatPanel />);
    expect(screen.queryByRole("button", { name: "Retry interrupted request" })).not.toBeInTheDocument();
  });

  it.each(["newer_request", "final_reply", "other_session", "still_composing"])(
    "withholds cancellation recovery for %s",
    (state) => {
      (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
        sendMessage: vi.fn(), retryMessage: vi.fn(),
        isComposing: state === "still_composing", error: null,
      });
      const user: ChatMessage = {
        id: "cancelled-request", session_id: "session-1", role: "user",
        content: "Assess the case studies", tool_calls: null,
        created_at: "2026-10-01T06:40:19Z",
      };
      const messages = [user];
      if (state === "newer_request" || state === "final_reply") {
        messages.push({
          ...user, id: "later-message",
          role: state === "newer_request" ? "user" : "assistant",
          content: "Later turn", created_at: "2026-10-01T06:45:00Z",
        });
      }
      useSessionStore.setState({
        activeSessionId: "session-1", messages,
        composerProgress: {
          session_id: state === "other_session" ? "session-2" : "session-1",
          request_id: user.id, phase: "cancelled", reason: "client_cancelled",
          headline: "Cancelled", evidence: [], likely_next: "Retry when ready.",
          updated_at: "2026-10-01T06:44:18Z", inflight_requests: 0,
        },
      });
      render(<ChatPanel />);
      expect(screen.queryByRole("button", { name: "Retry interrupted request" })).not.toBeInTheDocument();
    },
  );

  // The terminal snapshot bridges the gap between a turn settling and its
  // reply rendering, then must RETIRE. Nothing else clears composerProgress
  // until the next compose, so a bare `|| isTerminal` left this mounted
  // indefinitely — and because it is docked inside the composer's flex
  // column, it held its full completed-turn height in the input's space.
  // These two pin the retirement RULE (has the reply landed?), not the
  // symptom (how tall the box got).
  const terminalCompleteProgress: ComposerProgressSnapshot = {
    session_id: "session-1",
    request_id: "u1",
    phase: "complete",
    headline: "The composer has updated the pipeline.",
    evidence: ["The assistant response has been saved for this session."],
    likely_next: "Review the response and current pipeline.",
    reason: null,
    updated_at: "2026-04-26T10:00:02Z",
  };

  const idleComposer = {
    sendMessage: vi.fn(),
    retryMessage: vi.fn(),
    cancelComposition: vi.fn(),
    isComposing: false,
    compositionState: null,
    error: null,
  };

  const soloSession: Session = {
    id: "session-1",
    title: "Composer session",
    created_at: "2026-04-26T10:00:00Z",
    updated_at: "2026-04-26T10:00:00Z",
  };

  const chatMsg = (
    overrides: Partial<ChatMessage> & { id: string; role: ChatMessage["role"] },
  ): ChatMessage => ({
    session_id: "session-1",
    content: "",
    tool_calls: null,
    created_at: "2026-04-26T10:00:01Z",
    ...overrides,
  });

  it("retires terminal composer progress once the final assistant text lands", () => {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue(idleComposer);
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [soloSession],
      // A complete agent tail: an assistant row carrying non-empty content is
      // what flips ChatTurn.isComplete (see turns.ts).
      messages: [
        chatMsg({ id: "u1", role: "user", content: "Build me a pipeline" }),
        chatMsg({ id: "a1", role: "assistant", content: "Done — the pipeline is saved." }),
      ],
      composerProgress: terminalCompleteProgress,
    });

    render(<ChatPanel />);

    expect(screen.getByText("Done — the pipeline is saved.")).toBeInTheDocument();
    expect(screen.queryByText("Last composer update")).not.toBeInTheDocument();
    expect(
      screen.queryByText("The composer has updated the pipeline."),
    ).not.toBeInTheDocument();
  });

  it("does not resurrect terminal composer progress after a later system notice", () => {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue(idleComposer);
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [soloSession],
      // Validation notices are standalone system turns and can land after the
      // assistant reply. They must not make the settled progress card return.
      messages: [
        chatMsg({ id: "u1", role: "user", content: "Build me a pipeline" }),
        chatMsg({ id: "a1", role: "assistant", content: "Done — the pipeline is saved." }),
        chatMsg({ id: "s1", role: "system", content: "Validation passed." }),
      ],
      composerProgress: terminalCompleteProgress,
    });

    render(<ChatPanel />);

    expect(screen.getByText("Done — the pipeline is saved.")).toBeInTheDocument();
    expect(screen.getByText("Validation passed.")).toBeInTheDocument();
    expect(screen.queryByText("Last composer update")).not.toBeInTheDocument();
    expect(
      screen.queryByText("The composer has updated the pipeline."),
    ).not.toBeInTheDocument();
  });

  it("does not show terminal composer progress superseded by a later user turn", () => {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue(idleComposer);
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [soloSession],
      messages: [
        chatMsg({ id: "u0", role: "user", content: "Build the old pipeline" }),
        chatMsg({ id: "u1", role: "user", content: "Build the current pipeline" }),
      ],
      composerProgress: { ...terminalCompleteProgress, request_id: "u0" },
    });

    render(<ChatPanel />);

    expect(screen.getByText("Build the current pipeline")).toBeInTheDocument();
    expect(screen.queryByText("Last composer update")).not.toBeInTheDocument();
    expect(
      screen.queryByText("The composer has updated the pipeline."),
    ).not.toBeInTheDocument();
  });


  it.each(["pending", "failed"] as const)(
    "keeps terminal progress for an absent canonical id while the optimistic user row is %s",
    (localStatus) => {
      (useComposer as ReturnType<typeof vi.fn>).mockReturnValue(idleComposer);
      useSessionStore.setState({
        activeSessionId: "session-1",
        sessions: [soloSession],
        messages: [
          chatMsg({
            id: "local-u1",
            role: "user",
            content: "Build me a pipeline",
            local_status: localStatus,
          }),
        ],
        composerProgress: {
          ...terminalCompleteProgress,
          request_id: "canonical-u1",
          phase: "cancelled",
          headline: "Composition stopped before saving.",
          reason: "client_cancelled",
        },
      });

      render(<ChatPanel />);

      expect(screen.getByText("Last composer update")).toBeInTheDocument();
      expect(
        screen.getByText("Composition stopped before saving."),
      ).toBeInTheDocument();
    },
  );

  it("keeps terminal composer progress while the reply has not landed yet", () => {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue(idleComposer);
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [soloSession],
      // The latest user turn settled without reply text, so the snapshot is
      // still the only account of what happened. A successful older turn must
      // not cause this newer terminal outcome to be hidden.
      messages: [
        chatMsg({ id: "u0", role: "user", content: "Build an earlier pipeline" }),
        chatMsg({ id: "a0", role: "assistant", content: "The earlier pipeline is ready." }),
        chatMsg({ id: "u1", role: "user", content: "Build me a pipeline" }),
      ],
      composerProgress: terminalCompleteProgress,
    });

    render(<ChatPanel />);

    expect(screen.getByText("The composer has updated the pipeline.")).toBeInTheDocument();
  });

  it("scopes an unsent freeform draft to its session across switches", () => {
    // elspeth-ca38667856: ChatPanel stays mounted across session switches, so
    // an unscoped inputText leaked session A's unsent draft into session B's
    // composer. The draft is keyed by session id: invisible on B, restored on
    // returning to A (clearing on switch would destroy typed content, which
    // the elspeth-49b467d91a retention doctrine forbids).
    const sessionA: Session = {
      id: "session-a",
      title: "Freeform session A",
      created_at: "2026-04-26T10:00:00Z",
      updated_at: "2026-04-26T10:00:00Z",
    };
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-a",
      sessions: [sessionA],
      messages: [],
    });

    render(<ChatPanel />);

    act(() => {
      screen.getByTestId("chat-input-type").click();
    });
    expect(screen.getByTestId("chat-input").getAttribute("data-value")).toBe(
      "retyped while pending",
    );

    act(() => {
      useSessionStore.setState({ activeSessionId: "session-b" });
    });
    expect(screen.getByTestId("chat-input").getAttribute("data-value")).toBe("");

    act(() => {
      useSessionStore.setState({ activeSessionId: "session-a" });
    });
    expect(screen.getByTestId("chat-input").getAttribute("data-value")).toBe(
      "retyped while pending",
    );
  });

  it("shows the quiet introduction in an empty freeform session", () => {
    const sendMessage = vi.fn();
    const session: Session = {
      id: "session-templates",
      title: "Template session",
      created_at: "2026-04-26T10:00:00Z",
      updated_at: "2026-04-26T10:00:00Z",
    };

    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage,
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: session.id,
      sessions: [session],
      messages: [],
    });
    usePreferencesStore.setState({
      loaded: true,
      freeformIntroDismissedAt: null,
    });

    render(<ChatPanel />);

    expect(
      screen.getByRole("heading", { name: "How pipelines work" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Don't show this again" })).toBeVisible();
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("coalesces consecutive assistant rows into one agent turn bubble", () => {
    // Reproduces the live shape at session a8afd33e: one user prompt + seven
    // assistant rows (six with single tool_calls, one orphan empty) + a final
    // answer row. The backend persists each LLM round-trip as its own row
    // (Tier-1 audit doctrine), but the chat panel must render the user-visible
    // turn — one user bubble + one agent bubble carrying the aggregated tool
    // calls and the final answer.
    const session: Session = {
      id: "session-coalesce",
      title: "Coalesce session",
      created_at: "2026-05-19T00:00:00Z",
      updated_at: "2026-05-19T00:00:00Z",
    };
    const mkMsg = (overrides: Partial<ChatMessage> & { id: string; role: ChatMessage["role"] }): ChatMessage => ({
      session_id: session.id,
      content: "",
      tool_calls: null,
      created_at: "2026-05-19T00:00:00Z",
      ...overrides,
    }) as ChatMessage;
    const tc = (name: string, id = name) => ({ id, type: "function", function: { name, arguments: "{}" } });

    const messages: ChatMessage[] = [
      mkMsg({ id: "u1", role: "user", content: "create a list of 5 government web pages..." }),
      mkMsg({ id: "a1", role: "assistant", tool_calls: [tc("list_models"), tc("get_plugin_schema", "g1")] }),
      mkMsg({ id: "a2", role: "assistant", tool_calls: [tc("create_blob")] }),
      mkMsg({ id: "a3", role: "assistant", tool_calls: [tc("set_pipeline")] }),
      mkMsg({ id: "a4", role: "assistant", tool_calls: [tc("patch_node_options")] }),
      mkMsg({ id: "a5", role: "assistant", tool_calls: [tc("preview_pipeline", "p1")] }),
      mkMsg({ id: "a6", role: "assistant" }),
      mkMsg({ id: "a7", role: "assistant", tool_calls: [tc("preview_pipeline", "p2")] }),
      mkMsg({ id: "a8", role: "assistant", content: "Built a workflow that fetches each page and rates it." }),
    ];

    useSessionStore.setState({
      activeSessionId: session.id,
      sessions: [session],
      messages,
    });

    render(<ChatPanel />);

    // One bubble per turn: 1 user + 1 agent = 2 — not 9 (the audit row count).
    const bubbles = screen.getAllByTestId("message-bubble");
    expect(bubbles).toHaveLength(2);

    // Agent turn carries the final content from the LAST row in the turn,
    // not from any intermediate empty-content row.
    expect(
      screen.getByText("Built a workflow that fetches each page and rates it."),
    ).toBeInTheDocument();

    // User turn still renders its own bubble.
    expect(
      screen.getByText("create a list of 5 government web pages..."),
    ).toBeInTheDocument();
  });

  it("keeps a historical aborted agent turn visible while hiding the current incomplete turn", () => {
    const session: Session = {
      id: "session-aborted-turn",
      title: "Aborted turn session",
      created_at: "2026-08-02T00:00:00Z",
      updated_at: "2026-08-02T00:00:00Z",
    };
    const mkMsg = (
      overrides: Partial<ChatMessage> & {
        id: string;
        role: ChatMessage["role"];
      },
    ): ChatMessage =>
      ({
        session_id: session.id,
        content: "",
        tool_calls: null,
        created_at: "2026-08-02T00:00:00Z",
        ...overrides,
      }) as ChatMessage;
    const tc = (name: string) => ({
      id: name,
      type: "function",
      function: { name, arguments: "{}" },
    });

    useSessionStore.setState({
      activeSessionId: session.id,
      sessions: [session],
      messages: [
        mkMsg({ id: "u1", role: "user", content: "First request" }),
        mkMsg({ id: "a1", role: "assistant", tool_calls: [tc("first_tool")] }),
        mkMsg({ id: "u2", role: "user", content: "Second request" }),
        mkMsg({ id: "a2", role: "assistant", tool_calls: [tc("second_tool")] }),
      ],
    });

    render(<ChatPanel />);

    // The first incomplete agent turn is historical because a later user turn
    // exists, so it remains in the timeline. Only the current tail turn stays
    // behind the atomic-reveal gate while composition is in flight.
    expect(screen.getAllByTestId("message-bubble")).toHaveLength(3);
  });

  it("passes matching and stale proposal state to message bubbles", () => {
    const session: Session = {
      id: "session-1",
      title: "Proposal session",
      created_at: "2026-05-14T00:00:00Z",
      updated_at: "2026-05-14T00:00:00Z",
    };
    const assistantMessage: ChatMessage = {
      id: "assistant-1",
      session_id: "session-1",
      role: "assistant",
      content: "I need approval.",
      tool_calls: [
        {
          id: "call-1",
          type: "function",
          function: { name: "set_pipeline", arguments: "{}" },
        },
      ],
      created_at: "2026-05-14T00:00:01Z",
    };
    const proposal: CompositionProposal = {
      id: "proposal-1",
      session_id: "session-1",
      tool_call_id: "call-1",
      tool_name: "set_pipeline",
      status: "pending",
      summary: "Replace the pipeline.",
      rationale: "Requested by the current composer turn.",
      affects: ["graph"],
      arguments_redacted_json: {},
      base_state_id: null,
      committed_state_id: null,
      audit_event_id: "event-1",
      created_at: "2026-05-14T00:00:00Z",
      updated_at: "2026-05-14T00:00:00Z",
    };

    // The composer must be idle for this scenario to be coherent: a proposal
    // sitting for the user to accept/reject means the turn already returned.
    // Left composing, the atomic-reveal gate correctly hides the turn — an
    // assistant row carrying tool_calls is mid-loop narration, not a reply
    // (turns.ts -> isGenuineReply, elspeth-e074575b6e). This test inherited
    // isComposing:true from an earlier mockReturnValue.
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [assistantMessage],
      compositionProposals: [proposal],
      staleProposalIds: ["proposal-1"],
    });

    render(<ChatPanel />);

    expect(screen.getAllByText("Replace the pipeline.")).toHaveLength(2);
    expect(screen.getByText("Stale proposal")).toBeInTheDocument();
  });

  // ── Dock arrival mechanics (elspeth-2d1cf8908c) ──────────────────────────
  //
  // The dock is a scroll container by design (elspeth-ecf973fb9f), so a
  // PendingProposalsBanner mounting below its fold is silent: no live-region
  // announcement (the banner returns null when empty, so a role on the banner
  // itself would mount WITH its content — the unreliable pattern) and nothing
  // scrolling the dock to the new approval control. These pin both halves:
  // the persistent announcer and the scroll-the-dock-BY-NAME arrival scroll
  // (never scrollIntoView — its ancestor walk is the elspeth-ecf973fb9f bug).
  function makeArrivalProposal(id: string): CompositionProposal {
    return {
      id,
      session_id: "session-1",
      tool_call_id: `call-${id}`,
      tool_name: "set_pipeline",
      status: "pending",
      summary: "Replace the pipeline.",
      rationale: "Requested by the current composer turn.",
      affects: ["graph"],
      arguments_redacted_json: {},
      base_state_id: null,
      committed_state_id: null,
      audit_event_id: `event-${id}`,
      created_at: "2026-05-14T00:00:00Z",
      updated_at: "2026-05-14T00:00:00Z",
    };
  }

  function renderIdleFreeformPanel() {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [],
    });
    const { container } = render(<ChatPanel />);
    const dock = container.querySelector<HTMLElement>(".chat-panel-dock");
    expect(dock).not.toBeNull();
    return dock as HTMLElement;
  }

  it("scrolls the dock by name when a new actionable proposal arrives", async () => {
    const dock = renderIdleFreeformPanel();
    const scrollSpy = vi.spyOn(dock, "scrollTo");

    act(() => {
      useSessionStore.setState({
        compositionProposals: [makeArrivalProposal("proposal-1")],
      });
    });

    await waitFor(() => expect(scrollSpy).toHaveBeenCalledTimes(1));
    // The banner must be the scroll target's reason — and the mechanism must
    // be the named dock, never an ancestor-walking scrollIntoView.
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();

    // Arrival-keyed, not identity-keyed: an unrelated store change re-renders
    // the panel (and rebuilds the derived proposal arrays) but must not
    // re-scroll a banner the operator may have scrolled away from.
    act(() => {
      useSessionStore.setState({
        messages: [
          {
            id: "msg-1",
            session_id: "session-1",
            role: "user",
            content: "hello",
            tool_calls: null,
            created_at: "2026-05-14T00:00:02Z",
          },
        ],
      });
    });
    expect(scrollSpy).toHaveBeenCalledTimes(1);
  });

  it("reveals the rejection action when an obsolete proposal arrives", () => {
    const dock = renderIdleFreeformPanel();
    const scrollSpy = vi.spyOn(dock, "scrollTo");

    act(() => {
      useSessionStore.setState({
        compositionProposals: [makeArrivalProposal("proposal-1")],
        staleProposalIds: ["proposal-1"],
      });
    });

    expect(scrollSpy).toHaveBeenCalledTimes(1);
  });

  it("downgrades the arrival scroll to behavior:'auto' under prefers-reduced-motion (elspeth-5b42a9ae1e)", async () => {
    // The imperative scrollTo API is NOT auto-downgraded by the OS
    // preference the way CSS animations behind the media query are — every
    // JS scroll must consult it via preferredScrollBehavior(). This pins
    // one representative site; the helper's own spec pins the mechanism.
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)",
      media: query,
    })) as unknown as typeof window.matchMedia;
    try {
      const dock = renderIdleFreeformPanel();
      const scrollSpy = vi.spyOn(dock, "scrollTo");

      act(() => {
        useSessionStore.setState({
          compositionProposals: [makeArrivalProposal("proposal-1")],
        });
      });

      await waitFor(() => expect(scrollSpy).toHaveBeenCalledTimes(1));
      expect(scrollSpy).toHaveBeenCalledWith(
        expect.objectContaining({ behavior: "auto" }),
      );
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });


  it("announces a proposal arrival through the persistent live region", async () => {
    renderIdleFreeformPanel();

    // The region pre-exists its content — that is the property that makes the
    // 0→1 announcement reliable.
    const region = screen.getByTestId("decision-panel-live-region");
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveTextContent("");

    act(() => {
      useSessionStore.setState({
        compositionProposals: [makeArrivalProposal("proposal-1")],
      });
    });

    await waitFor(() => expect(region).toHaveTextContent("1 item needs your decision"));
  });
});



// ── Inline-source projection (Phase 5a Task 3) ────────────────────────────────
//
// These tests cover the wiring that derives an InlineSourceSummary from
// `compositionState.sources[*].options["blob_ref"]` and the corresponding session
// blob's metadata + preview, then surfaces the InlineSourceCreatedTurn widget
// in the message stream.
//
// The widget itself is tested in InlineSourceCreatedTurn.test.tsx; here we
// only assert the predicate ("widget renders iff inline source is bound to
// the active session") and the absence case ("no inline source → no
// widget"). Detailed widget rendering, edit-button visibility per
// provenance, and audit-info disclosure all live in the widget test.
describe("ChatPanel inline-source projection", () => {
  const twoRowInlineSourceText = "url\nhttps://a.gov.au\nhttps://b.gov.au";
  const twoRowInlineSourceHash =
    "9b8d3393ad3be052da5f25595789f926a161a4f8c0090c61f10a9cbab69a473c";
  const oneRowInlineSourceText = "url\nhttps://a.gov.au";
  const differentInlineSourceHash =
    "e14713c61f9a7d0119925f46e9957e6d42a1604a5d62932853c46b03681af30b";

  const sessionFixture: Session = {
    id: "session-inline",
    title: "Inline session",
    created_at: "2026-05-18T10:00:00Z",
    updated_at: "2026-05-18T10:00:00Z",
  };

  function makeBlobMetadata(overrides: Partial<BlobMetadata> = {}): BlobMetadata {
    return {
      id: "blob-inline-1",
      session_id: "session-inline",
      filename: "chat.csv",
      mime_type: "text/csv",
      size_bytes: 42,
      content_hash: twoRowInlineSourceHash,
      created_at: "2026-05-18T10:00:01Z",
      created_by: "assistant",
      source_description: null,
      status: "ready",
      creation_modality: "llm_generated",
      created_from_message_id: "msg-1",
      creating_model_identifier: "claude-opus-4-7",
      creating_model_version: "20260101",
      creating_provider: "anthropic",
      creating_composer_skill_hash: "skill-hash",
      creating_arguments_hash: "args-hash",
      ...overrides,
    };
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useInlineSourceStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
  });

  it("renders the widget when a composition source blob_ref resolves to a session blob", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata(),
    );
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue(twoRowInlineSourceText);

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(
        screen.getByRole("region", { name: /source created/i }),
      ).toBeInTheDocument();
    });

    // Provenance-derived: llm_generated → llm-generated (display form) →
    // Edit affordance present (F-4). Asserted here AS A WIRING TEST to
    // confirm the projection carries provenance end-to-end; the widget's
    // own test owns the per-provenance rendering matrix.
    expect(
      screen.getByRole("button", { name: /edit the list/i }),
    ).toBeInTheDocument();
  });

  it("renders the widget when a named inline source carries the blob_ref", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata(),
    );
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue(twoRowInlineSourceText);

    const composition = makeComposition(1, {
      sources: {
        created: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(apiClient.getBlobMetadata).toHaveBeenCalledWith(
        "session-inline",
        "blob-inline-1",
      );
      expect(
        screen.getByRole("region", { name: /source created/i }),
      ).toBeInTheDocument();
    });
  });

  it("does NOT fetch content for an uploaded source blob_ref", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata({
        created_by: "user",
        source_description: "uploaded",
        creation_modality: "verbatim",
        created_from_message_id: null,
        creating_model_identifier: null,
        creating_model_version: null,
        creating_provider: null,
        creating_composer_skill_hash: null,
        creating_arguments_hash: null,
        size_bytes: 250_000_000,
      }),
    );

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "csv_file",
          options: { blob_ref: "blob-inline-1", path: "/data/upload.csv" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(apiClient.getBlobMetadata).toHaveBeenCalledWith(
        "session-inline",
        "blob-inline-1",
      );
    });

    expect(apiClient.previewBlobContent).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();
  });

  it("does NOT render the widget when compositionState has no inline source", () => {
    const composition = makeComposition(1, {
      sources: { source: { plugin: "csv_file", options: { path: "data.csv" } } },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();
    // No blob fetch attempted when no blob_ref is present.
    expect(apiClient.getBlobMetadata).not.toHaveBeenCalled();
    expect(apiClient.previewBlobContent).not.toHaveBeenCalled();
  });

  it("clears a stale inline-source summary when the active blob is not inline", async () => {
    useInlineSourceStore.getState().setSummary("session-inline", {
      blobId: "old-inline",
      filename: "old.csv",
      mimeType: "text/csv",
      contentPreview: "url\nhttps://old.gov.au",
      rowCount: 2,
      contentHash: twoRowInlineSourceHash,
      provenance: "llm-generated",
    });
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata({
        id: "uploaded-blob",
        created_by: "user",
        created_from_message_id: null,
      }),
    );
    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "csv_file",
          options: { blob_ref: "uploaded-blob" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(useInlineSourceStore.getState().getSummaries("session-inline")[0]).toBeUndefined();
    });
    expect(apiClient.previewBlobContent).not.toHaveBeenCalled();
  });

  it("does NOT render the widget when compositionState has no sources", () => {
    const composition = makeComposition(1, { sources: {} });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();
  });

  // Tier-1 audit-trail invariant (see InlineSourceSummary.contentHash type
  // doc): a blob with a null content_hash is a wire-contract violation.
  // The projection effect throws on this case; the throw is caught and
  // logged; the inlineSourceStore is NEVER populated; the widget does
  // NOT render.  Substituting an empty string into the rendered audit
  // pane would assert a value the system never recorded: absence is
  // evidence, and a fabricated hash is indistinguishable from a real one.
  it("does NOT render the widget when the blob's content_hash is null (audit-trail invariant)", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...makeBlobMetadata(),
      content_hash: null,
    });
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue("url\nhttps://a.gov.au");

    // Suppress the expected console.error from the projection's catch
    // arm so the test output is clean.  The assertion below confirms
    // that the error WAS logged with the expected prefix — that's how
    // we know the invariant fired rather than the test silently
    // matching the negative case for an unrelated reason.
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    // Wait for the projection effect to resolve and throw.
    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(
        expect.stringMatching(/\[inline-source\] projection failed:/),
        expect.any(Error),
      );
    });

    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();

    errorSpy.mockRestore();
  });

  it("does NOT render the widget when blob MIME metadata has malformed parameter syntax", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata({ mime_type: "text/csv; charset=" }),
    );
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue(twoRowInlineSourceText);
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(
        expect.stringMatching(/\[inline-source\] projection failed:/),
        expect.objectContaining({
          message: expect.stringMatching(/invalid MIME metadata/i),
        }),
      );
    });

    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();
    expect(
      useInlineSourceStore.getState().getSummaries("session-inline")[0],
    ).toBeUndefined();

    errorSpy.mockRestore();
  });

  it("does NOT render the widget when preview bytes disagree with blob metadata content_hash", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata({ content_hash: differentInlineSourceHash }),
    );
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue(oneRowInlineSourceText);
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(
        expect.stringMatching(/\[inline-source\] projection failed:/),
        expect.objectContaining({
          message: expect.stringMatching(/content_hash mismatch/i),
        }),
      );
    });

    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();
    expect(
      useInlineSourceStore.getState().getSummaries("session-inline")[0],
    ).toBeUndefined();

    errorSpy.mockRestore();
  });

  it("logs the bound projection error object when provenance translation throws", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBlobMetadata(),
    );
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue(twoRowInlineSourceText);
    const projectionError = new TypeError("projection dependency failed");
    vi.spyOn(apiClient, "toInlineSourceProvenance").mockImplementation(() => {
      throw projectionError;
    });
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-inline-1" },
        },
      },
    });

    useSessionStore.setState({
      activeSessionId: "session-inline",
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(
        expect.stringMatching(/\[inline-source\] projection failed:/),
        projectionError,
      );
    });
    expect(
      screen.queryByRole("region", { name: /source created/i }),
    ).toBeNull();

    errorSpy.mockRestore();
  });
});





// Inline source proposals use the same review and custody path as every
// other pipeline proposal, regardless of assistant interpretation prose.
describe("ChatPanel generic inline-source proposal review", () => {
  const sessionFixture: Session = {
    id: "session-source-review",
    title: "Source review session",
    created_at: "2026-05-18T10:00:00Z",
    updated_at: "2026-05-18T10:00:00Z",
  };

  function makeProposalAndMessages() {
    const recorded = redactedCase("set_pipeline_ambiguous_inline_omitted_metadata");
    const userMessage: ChatMessage = {
      id: "user-source-review",
      session_id: sessionFixture.id,
      role: "user",
      // Deliberately has more text lines than records in the proposed artifact.
      content: "Ava — access problem\nstill cannot sign in\nBen — invoice wrong\nbilled twice",
      tool_calls: null,
      created_at: "2026-05-18T10:00:00Z",
    };
    const assistantMessage: ChatMessage = {
      id: "assistant-source-review",
      session_id: sessionFixture.id,
      role: "assistant",
      content: "I read those notes as records for review.",
      tool_calls: [{
        id: "tool-source-review",
        type: "function",
        function: { name: recorded.tool, arguments: JSON.stringify(recorded.arguments) },
      }],
      created_at: "2026-05-18T10:00:01Z",
    };
    const proposal: CompositionProposal = {
      id: "proposal-source-review",
      session_id: sessionFixture.id,
      tool_call_id: "tool-source-review",
      tool_name: recorded.tool,
      status: "pending",
      summary: recorded.proposal_summary,
      rationale: "",
      affects: ["source"],
      arguments_redacted_json: structuredClone(recorded.redacted),
      base_state_id: null,
      committed_state_id: null,
      audit_event_id: null,
      created_at: "2026-05-18T10:00:00Z",
      updated_at: "2026-05-18T10:00:00Z",
    };
    return { proposal, userMessage, assistantMessage };
  }

  function seedReview() {
    const fixture = makeProposalAndMessages();
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [fixture.userMessage, fixture.assistantMessage],
      compositionProposals: [fixture.proposal],
    });
    return fixture;
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useInlineSourceStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
  });

  it("renders the producer's omitted-metadata proposal once in the generic banner without guessing user-text row counts", () => {
    const { proposal, userMessage, assistantMessage } = seedReview();
    expect(proposal.arguments_redacted_json).not.toHaveProperty("metadata");
    render(<ChatPanel />);

    const banners = screen.getAllByRole("region", { name: "Awaiting your decision (1)" });
    expect(banners).toHaveLength(1);
    expect(within(banners[0]).getAllByRole("listitem")).toHaveLength(1);
    expect(within(banners[0]).getByText(proposal.summary)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /row count/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /yes.*rows|one row|not source data/i })).not.toBeInTheDocument();
    expect(useSessionStore.getState().compositionProposals).toEqual([proposal]);
    expect(useSessionStore.getState().messages).toEqual([userMessage, assistantMessage]);
  });

  it.each(["I read your message as 4 rows.", "Input interpreted as 4 rows."])(
    "keeps legacy interpretation narration on the generic review path: %s",
    (summary) => {
      const { proposal } = seedReview();
      useSessionStore.setState({ compositionProposals: [{ ...proposal, summary }] });
      render(<ChatPanel />);
      expect(screen.getByRole("region", { name: "Awaiting your decision (1)" })).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: /row count/i })).not.toBeInTheDocument();
    },
  );

  it.each(["accept", "reject"] as const)(
    "%s delegates the exact original proposal ID without replacing its base or transcript associations",
    async (action) => {
      const { proposal, userMessage, assistantMessage } = seedReview();
      const acceptProposal = vi.fn().mockResolvedValue(undefined);
      const rejectProposal = vi.fn().mockResolvedValue(undefined);
      useSessionStore.setState({ acceptProposal, rejectProposal });
      render(<ChatPanel />);
      const banner = screen.getByRole("region", { name: "Awaiting your decision (1)" });
      if (action === "accept") {
        fireEvent.click(within(banner).getByRole("button", { name: `Accept proposal: ${proposal.summary}` }));
        expect(acceptProposal).toHaveBeenCalledExactlyOnceWith(proposal.id);
        expect(rejectProposal).not.toHaveBeenCalled();
      } else {
        fireEvent.click(within(banner).getByRole("button", { name: `Reject proposal: ${proposal.summary}` }));
        expect(rejectProposal).not.toHaveBeenCalled();
        const dialog = await screen.findByRole("alertdialog", { name: "Reject proposal" });
        fireEvent.click(within(dialog).getByRole("button", { name: "Reject proposal" }));
        expect(rejectProposal).toHaveBeenCalledExactlyOnceWith(proposal.id);
        expect(acceptProposal).not.toHaveBeenCalled();
      }
      expect(useSessionStore.getState().compositionProposals).toEqual([proposal]);
      expect(proposal.base_state_id).toBeNull();
      expect(proposal.tool_call_id).toBe(assistantMessage.tool_calls![0].id);
      expect(useSessionStore.getState().messages).toEqual([userMessage, assistantMessage]);
      expect(useComposer().sendMessage).not.toHaveBeenCalled();
    },
  );

  it("retains rejection and the tool association for obsolete proposals", () => {
    const { proposal } = seedReview();
    useSessionStore.setState({
      compositionProposals: [{ ...proposal, base_state_id: "previous-state" }],
      staleProposalIds: [proposal.id],
    });
    render(<ChatPanel />);
    expect(screen.queryByRole("region", { name: /pending changes|row count/i })).not.toBeInTheDocument();
    expect(screen.getByText("Stale proposal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /accept proposal/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject proposal/i })).toBeEnabled();
  });

  it("keeps in-flight proposal actions disabled", () => {
    const { proposal } = seedReview();
    useSessionStore.setState({ proposalActionPendingIds: [proposal.id] });
    render(<ChatPanel />);
    const banner = screen.getByRole("region", { name: "Awaiting your decision (1)" });
    expect(within(banner).getByRole("button", { name: /accept proposal/i })).toBeDisabled();
    expect(within(banner).getByRole("button", { name: /reject proposal/i })).toBeDisabled();
  });

  it("preserves generic review for a source proposal that also replaces outputs and existing composition", () => {
    const { proposal } = seedReview();
    const replacementProposal = {
      ...proposal,
      base_state_id: "existing-state",
      arguments_redacted_json: {
        ...proposal.arguments_redacted_json,
        outputs: [{ sink_name: "result", plugin: "json", options: { path: "<redacted-option-value>" } }],
      },
    };
    useSessionStore.setState({
      compositionProposals: [replacementProposal],
      compositionState: makeComposition(1),
    });
    render(<ChatPanel />);
    expect(screen.getByRole("region", { name: "Awaiting your decision (1)" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /row count/i })).not.toBeInTheDocument();
    expect(useSessionStore.getState().compositionProposals).toEqual([replacementProposal]);
  });
});

// ── No "Create source" offer for typed input (ruling 2026-10-08) ─────────────
//
// The chat used to offer any recent user message containing a URL (or a short
// comma list) back as source data, and its "Create source" button resent the
// WHOLE message prefixed "Use this as my source data:". The first-run tutorial
// brief carries three URLs, so a learner who reloaded mid-compose was offered
// their own instructions as data and sent the brief twice. Creating a source
// from typed input is the composer's job; the chat makes no such offer.
describe("ChatPanel makes no inline-source offer for typed input", () => {
  const sessionFixture: Session = {
    id: "session-no-source-offer",
    title: "No source offer",
    created_at: "2026-10-08T03:04:34Z",
    updated_at: "2026-10-08T03:04:34Z",
  };

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useInlineSourceStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
  });

  it("offers nothing for an idle, source-less session whose instruction contains URLs", () => {
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [{
        id: "user-brief",
        session_id: sessionFixture.id,
        role: "user",
        content:
          "Build a pipeline to scrape and summarize these project briefs.\n\n" +
          "https://example.gov.au/project-1.html\nhttps://example.gov.au/project-2.html",
        tool_calls: null,
        created_at: "2026-10-08T03:04:36Z",
      }],
      compositionState: null,
    });

    render(<ChatPanel />);

    expect(screen.queryByRole("button", { name: /create source/i })).toBeNull();
    expect(screen.queryByText(/looks like source data/i)).toBeNull();
    expect(screen.queryByRole("region", { name: /awaiting your decision/i })).toBeNull();
  });
});


describe("ChatPanel interpretation-review inline-message dispatch", () => {
  const sessionFixture: Session = {
    id: "session-interp",
    title: "Interp session",
    created_at: "2026-05-18T10:00:00Z",
    updated_at: "2026-05-18T10:00:00Z",
  };

  function makeInterpretationEvent(
    overrides: Partial<InterpretationEvent> = {},
  ): InterpretationEvent {
    return {
      id: "evt-a",
      session_id: "session-interp",
      composition_state_id: "state-1",
      affected_node_id: "node-1",
      tool_call_id: "tool-1",
      user_term: "cool",
      kind: "vague_term",
      llm_draft: "trendy",
      accepted_value: null,
      choice: "pending",
      created_at: "2026-05-18T10:00:01Z",
      resolved_at: null,
      actor: "user:owner:u-1",
      interpretation_source: "user_approved",
      model_identifier: "anthropic/claude-opus-4-7",
      model_version: "20260518",
      provider: "anthropic",
      composer_skill_hash: "deadbeef",
      arguments_hash: null,
      hash_domain_version: null,
      runtime_model_identifier_at_resolve: null,
      runtime_model_version_at_resolve: null,
      approved_prompt_artifact_hash: null,
      ...overrides,
    };
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useInlineSourceStore);
    resetStore(useInterpretationEventsStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
  });

  it("updates the persisted handoff notice when its pending review resolves", () => {
    const notice = "Interpretation review cards are ready for this pipeline. Review the pending assumptions to continue.";
    const event = makeInterpretationEvent({
      session_id: sessionFixture.id,
      created_at: "2026-05-18T10:00:01Z",
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [
        {
          id: "user-1", session_id: sessionFixture.id, role: "user",
          content: "Build a pipeline", tool_calls: null, created_at: "2026-05-18T10:00:00Z",
        },
        {
          id: "assistant-1", session_id: sessionFixture.id, role: "assistant",
          content: `Pipeline summary\n\n${notice}`, tool_calls: null,
          created_at: "2026-05-18T10:00:02Z",
          segments: [
            { kind: "text", content: "Pipeline summary" },
            { kind: "trusted_system_notice", content: notice },
          ],
        },
      ],
    });
    useInterpretationEventsStore.setState({
      pendingBySession: { [sessionFixture.id]: { [event.id]: event } },
    });
    render(<ChatPanel />);
    expect(screen.getAllByTestId("message-bubble")[1]).toHaveAttribute(
      "data-pending-review-created-at", '["2026-05-18T10:00:01Z"]',
    );

    act(() => {
      useInterpretationEventsStore.setState({
        pendingBySession: { [sessionFixture.id]: {} },
        resolvedBySession: {
          [sessionFixture.id]: [{
            ...event, choice: "accepted_as_drafted", accepted_value: "accepted meaning",
            resolved_at: "2026-05-18T10:03:00Z",
          }],
        },
      });
    });
    expect(screen.getAllByTestId("message-bubble")[1]).toHaveAttribute("data-pending-review-created-at", "[]");
  });

  // Test 13: freeform mode + pending event → inline message rendered.
  it("renders an inline interpretation message in freeform mode when a pending event exists", () => {
    const event = makeInterpretationEvent();
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });
    act(() => {
      useInterpretationEventsStore
        .getState()
        .addPendingEvent(sessionFixture.id, event);
    });

    render(<ChatPanel />);

    expect(
      screen.getByTestId("acknowledgement-card"),
    ).toBeInTheDocument();
  });


  // Test 15: two pending events → two inline messages in created_at-ascending order.
  it("renders two inline messages in created_at-ascending order when two pending events exist", () => {
    // Seed in reverse-chronological order to ensure the component sorts
    // them (not just renders them in insertion order).
    const eventLater = makeInterpretationEvent({
      id: "evt-later",
      user_term: "later-term",
      created_at: "2026-05-18T11:00:00Z",
    });
    const eventEarlier = makeInterpretationEvent({
      id: "evt-earlier",
      user_term: "earlier-term",
      created_at: "2026-05-18T10:00:00Z",
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });
    act(() => {
      useInterpretationEventsStore
        .getState()
        .addPendingEvent(sessionFixture.id, eventLater);
      useInterpretationEventsStore
        .getState()
        .addPendingEvent(sessionFixture.id, eventEarlier);
    });

    render(<ChatPanel />);

    const widgets = screen.getAllByTestId(
      "acknowledgement-card",
    );
    expect(widgets).toHaveLength(2);
    // The earlier-created event renders first (top-of-list).  Match by the
    // user_term text inside each widget so the assertion does not depend on
    // event-id ordering, which would be a fragile proxy.
    expect(widgets[0].textContent).toMatch(/earlier-term/);
    expect(widgets[1].textContent).toMatch(/later-term/);
  });

  // Test 16: after opt-out the pending map is cleared → no inline messages.
  it("renders no inline messages after opt-out clears the pending map locally", async () => {
    const event = makeInterpretationEvent();
    // Mock the opt-out API call so the store action completes
    // synchronously-as-far-as-the-store-is-concerned.
    const optOutSpy = vi
      .spyOn(apiClient, "optOutOfInterpretations")
      .mockResolvedValue({
        session_id: sessionFixture.id,
        interpretation_review_disabled: true,
        opted_out_at: "2026-05-18T12:00:00Z",
      });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });
    act(() => {
      useInterpretationEventsStore
        .getState()
        .addPendingEvent(sessionFixture.id, event);
    });

    const { rerender } = render(<ChatPanel />);
    expect(
      screen.getByTestId("acknowledgement-card"),
    ).toBeInTheDocument();

    // Drive the opt-out via the store action — same surface the widget's
    // "Stop reviewing" confirm modal calls into.  The store clears
    // pendingBySession[sessionId] on success.
    await act(async () => {
      await useInterpretationEventsStore.getState().optOut(sessionFixture.id);
    });
    rerender(<ChatPanel />);

    expect(optOutSpy).toHaveBeenCalledWith(sessionFixture.id);
    expect(
      screen.queryByTestId("acknowledgement-card"),
    ).not.toBeInTheDocument();
  });

  // Test 17: negative-case routing predicate. An inline_blob proposal whose
  // summary contains neither "I read" nor "interpreted as" routes to the
  // standard InlineSourceCreatedTurn, NOT to this widget.  This pins the
  // discriminator between the two surfaces: the interpretation-review
  // widget keys off pendingInterpretationEvents (which is empty here),
  // and the InlineSourceCreatedTurn keys off inlineSourceSummary (which
  // we seed via the blob projection).
  it("an inline_blob proposal with no interpretation-context summary routes to InlineSourceCreatedTurn, not the interpretation-review inline message", async () => {
    (apiClient.getBlobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "blob-routing-1",
      session_id: sessionFixture.id,
      filename: "rows.csv",
      mime_type: "text/csv",
      size_bytes: 32,
      content_hash:
        "bb34d52cc97aefb5ce4513edda086520863c513bd8f3bd9165404000347d1081",
      created_at: "2026-05-18T10:00:01Z",
      created_by: "assistant",
      source_description: null,
      status: "ready",
      // Provenance is llm_generated (not interpretation-related).  The
      // resulting summary in the InlineSourceCreatedTurn body reads
      // "Created a 5-row source from your input" — i.e., it does NOT
      // contain "I read" or "interpreted as".
      creation_modality: "llm_generated",
      created_from_message_id: "msg-1",
      creating_model_identifier: "claude-opus-4-7",
      creating_model_version: "20260101",
      creating_provider: "anthropic",
      creating_composer_skill_hash: "skill-hash",
      creating_arguments_hash: "args-hash",
    });
    (
      apiClient.previewBlobContent as ReturnType<typeof vi.fn>
    ).mockResolvedValue("a\nb\nc\nd\ne\nf");

    const composition = makeComposition(1, {
      sources: {
        source: {
          plugin: "inline_blob",
          options: { blob_ref: "blob-routing-1" },
        },
      },
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
      compositionState: composition,
    });
    // NO pending interpretation event seeded.  An inline_blob proposal
    // without interpretation context does NOT produce a pending
    // interpretation event on the wire, so pendingBySession is empty.

    render(<ChatPanel />);

    await waitFor(() => {
      expect(
        screen.getByTestId("inline-source-created-turn"),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByTestId("acknowledgement-card"),
    ).not.toBeInTheDocument();
  });

  // ── Phase 5b.18b.8 resolve-success confirmation copy ──────────────────────
  //
  // After the inline-review widget resolves (Use mine / Submit amend), the
  // chat shows a short assistant-styled confirmation line so the user has
  // a closure cue. The widget unmounts on resolve (pendingBySession clears
  // the event); the confirmation lives in ChatPanel-local state captured
  // via onResolved BEFORE unmount.
  //
  // Spec lines 768-774: "Got it — using your interpretation of *<user_term>*."
  // — pure UI nudge, NOT persisted to the audit trail (which already
  // recorded the resolved interpretation_event row).
  it("offers one explicit planner repair when resolving a review reveals a graph error", async () => {
    resetStore(useExecutionStore);
    useExecutionStore.setState({ validate: useExecutionStore.getInitialState().validate });
    useSessionStore.setState({ applyResolvedInterpretation: useSessionStore.getInitialState().applyResolvedInterpretation });
    const event = makeInterpretationEvent({ user_term: "category" });
    vi.spyOn(apiClient, "listInterpretationEvents").mockResolvedValue([]);
    const message = "Edge contract violation: attach_sla emits Any but tidy_columns requires str for category.";
    const suggestion = "Declare the preserved category field in attach_sla's output schema.";
    vi.spyOn(apiClient, "resolveInterpretation").mockResolvedValue({
      event: { ...event, choice: "accepted_as_drafted" },
      new_state: makeComposition(8, { is_valid: false }),
    });
    vi.spyOn(apiClient, "validatePipeline").mockResolvedValue({
      is_valid: false, checks: [], warnings: [],
      errors: [{ component_id: "tidy_columns", component_type: "transform", message, suggestion }],
      readiness: {
        authoring_valid: true, execution_ready: false, completion_ready: false,
        blockers: [{ code: "graph_structure", component_id: "tidy_columns", component_type: "transform", detail: "Graph validation failed.", suggestion: null, note: null }],
      },
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id, sessions: [sessionFixture], messages: [],
      compositionState: makeComposition(7), composeTimeoutReady: true,
    });
    useInterpretationEventsStore.getState().addPendingEvent(sessionFixture.id, event);
    render(<ChatPanel />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /Acknowledge the LLM's interpretation/i })));

    const repair = await screen.findByRole("button", { name: "Ask composer to repair" });
    expect(useComposer().sendMessage).not.toHaveBeenCalled();
    fireEvent.click(repair);
    expect(useComposer().sendMessage).toHaveBeenCalledOnce();
    expect(useComposer().sendMessage).toHaveBeenCalledWith(expect.stringContaining(message));
    expect(useComposer().sendMessage).toHaveBeenCalledWith(expect.stringContaining(suggestion));
    expect(useComposer().sendMessage).toHaveBeenCalledWith(expect.stringContaining("Repair the pipeline"));
  });

  it("refreshes freeform readiness after resolving an interpretation", async () => {
    const event = makeInterpretationEvent({ user_term: "cool" });
    const next = makeComposition(2);
    vi.spyOn(apiClient, "resolveInterpretation").mockResolvedValue({
      event: { ...event, choice: "accepted_as_drafted" }, new_state: next,
    });
    const applyResolvedInterpretation = vi.fn();
    useSessionStore.setState({
      activeSessionId: sessionFixture.id, sessions: [sessionFixture], messages: [],
      applyResolvedInterpretation,
    });
    useInterpretationEventsStore.getState().addPendingEvent(sessionFixture.id, event);
    render(<ChatPanel />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /Acknowledge the LLM's interpretation/i })));
    expect(applyResolvedInterpretation).toHaveBeenCalledWith(next);
  });

  it("renders a resolve-success confirmation line after the user resolves an interpretation (Phase 5b.18b.8)", async () => {
    const event = makeInterpretationEvent({ user_term: "cool" });
    // Stub the resolve API so the store's resolveEvent action completes.
    const resolveSpy = vi
      .spyOn(apiClient, "resolveInterpretation")
      .mockResolvedValue({
        event: { ...event, choice: "accepted_as_drafted" },
        new_state: makeComposition(2),
      });

    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });
    act(() => {
      useInterpretationEventsStore
        .getState()
        .addPendingEvent(sessionFixture.id, event);
    });

    render(<ChatPanel />);

    // Initially the inline widget is mounted; no confirmation yet.
    expect(
      screen.getByTestId("acknowledgement-card"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("interpretation-review-confirmation"),
    ).not.toBeInTheDocument();

    // Click "Use my interpretation" — the widget calls the store's
    // resolveEvent, which calls api.resolveInterpretation, which our
    // spy resolves; on resolution the widget fires its onResolved
    // callback, ChatPanel records the confirmation, and the widget
    // unmounts (its event was removed from pendingBySession).
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: /Acknowledge the LLM's interpretation/i,
        }),
      );
    });

    // Confirmation copy is now visible; widget is gone.
    const confirmation = await screen.findByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmation.textContent).toMatch(
      /Got it — using your interpretation of/i,
    );
    expect(confirmation.textContent).toMatch(/cool/);
    expect(
      screen.queryByTestId("acknowledgement-card"),
    ).not.toBeInTheDocument();
    expect(resolveSpy).toHaveBeenCalledWith(
      sessionFixture.id,
      event.id,
      { choice: "accepted_as_drafted" },
    );
  });

  it("does not repeat approved interpretations in chat when the graph approval table is available", () => {
    useInterpretationEventsStore.setState({
      resolvedBySession: {
        [sessionFixture.id]: [makeInterpretationEvent({
          id: "approved-prompt", session_id: sessionFixture.id,
          tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}prompt`,
          kind: "llm_prompt_template", user_term: "llm_prompt_template:generate_text",
          affected_node_id: "generate_text", choice: "accepted_as_drafted",
          resolved_at: "2026-05-18T10:06:00Z",
        })],
      },
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      compositionState: makeComposition(1),
      messages: [],
    });
    render(<ChatPanel />);
    expect(screen.queryByTestId("interpretation-approvals-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interpretation-review-confirmation")).not.toBeInTheDocument();
  });

  // ── elspeth-51ed4fd8d5: anchoring and survival ────────────────────────────
  //
  // The confirmation used to be ChatPanel-local state appended to a list and
  // rendered after the whole turn stream. Append order WAS the position, so
  // the bubble was permanently last: resolve a card, send another message, and
  // "Got it" sat below that message reading as a reply to it, with several
  // resolutions piling up as a block at the tail. It was also never hydrated,
  // so a reload erased the operator's approvals from the transcript entirely.
  //
  // Both tests below fail against that implementation, and neither is
  // satisfied by "a confirmation is somewhere on screen" — the two assertions
  // the old tests made.

  function messagesRaisingToolCall(toolCallId: string): ChatMessage[] {
    const mk = (
      overrides: Partial<ChatMessage> & {
        id: string;
        role: ChatMessage["role"];
      },
    ): ChatMessage =>
      ({
        session_id: sessionFixture.id,
        content: "",
        tool_calls: null,
        created_at: "2026-05-18T10:00:00Z",
        ...overrides,
      }) as ChatMessage;
    return [
      mk({ id: "u1", role: "user", content: "make me a leads csv" }),
      mk({
        id: "a1",
        role: "assistant",
        tool_calls: [
          {
            id: toolCallId,
            type: "function",
            function: { name: "set_pipeline", arguments: "{}" },
          },
        ],
      }),
      mk({ id: "a2", role: "assistant", content: "Pipeline update ready." }),
      mk({ id: "u2", role: "user", content: "now rate each lead" }),
    ];
  }

  it("anchors the confirmation to the turn that raised the term, not the tail", () => {
    // The resolved row arrives the way refreshAll delivers it on load.
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              session_id: sessionFixture.id,
              tool_call_id: "call-set-pipeline",
              user_term: "inline_source_data",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:05:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const confirmation = screen.getByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmation.textContent).toMatch(/inline_source_data/);

    // Document order is the assertion — MessageBubble is mocked in this file,
    // so its wrapper classes are not available to anchor on and would be a
    // mock artefact if they were.
    //
    // The reported defect exactly: the confirmation must come BEFORE the user
    // turn that was sent afterwards, or it reads as a reply to that turn.
    const laterUserTurn = screen.getByText("now rate each lead");
    expect(
      confirmation.compareDocumentPosition(laterUserTurn) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // …and AFTER the turn that raised it, so it reads as that turn's closure
    // rather than as a preamble to it.
    const agentTurn = screen.getByText("Pipeline update ready.");
    expect(
      agentTurn.compareDocumentPosition(confirmation) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("rebuilds confirmations from the store, so a reload does not erase them", () => {
    // No interaction at all — this is a fresh mount reading what refreshAll
    // fetched, i.e. the state after a page reload. The old implementation
    // rendered nothing here: its list was seeded [] and only ever appended to
    // by an onResolved callback that a reload never fires.
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-1",
              session_id: sessionFixture.id,
              tool_call_id: "call-set-pipeline",
              user_term: "quality",
              choice: "accepted_as_drafted",
            }),
            makeInterpretationEvent({
              id: "evt-2",
              session_id: sessionFixture.id,
              // No anchor available: still shown, after the stream, rather
              // than dropped — an approval the operator gave is not discarded
              // for want of a place to put it.
              tool_call_id: null,
              user_term: "rate_lead_quality",
              choice: "amended",
            }),
            makeInterpretationEvent({
              id: "evt-3",
              session_id: sessionFixture.id,
              // Opt-out rows carry no term and must stay silent — the opt-out
              // flow has its own confirm dialog.
              tool_call_id: null,
              user_term: null,
              choice: "opted_out",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const confirmations = screen.getAllByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmations).toHaveLength(2);
    expect(confirmations.map((node) => node.textContent).join(" ")).toMatch(
      /quality[\s\S]*rate_lead_quality/,
    );
  });

  it("stays silent for surface-specific auto_interpreted_opt_out rows even though they carry a term and an anchor (elspeth-3a8a843c47)", () => {
    // The dangerous shape: when the session has opted out of interpretation
    // review and the composer LLM later calls request_interpretation_review,
    // the backend writes a born-resolved row that
    // ck_interpretation_events_opt_out_shape REQUIRES to carry a non-null
    // user_term AND tool_call_id — the audit trail must record what was baked
    // without review. Field presence therefore marks a DECLINED review, not
    // an approval; rendering "Got it — using your interpretation of <term>."
    // for it asserts an approval the operator explicitly refused to give.
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-auto-baked",
              session_id: sessionFixture.id,
              tool_call_id: "call-set-pipeline",
              user_term: "engagement",
              choice: "opted_out",
              interpretation_source: "auto_interpreted_opt_out",
              accepted_value: "trendy",
              resolved_at: "2026-05-18T10:05:00Z",
              actor: "composer-llm",
            }),
            // A genuine approval alongside it: the fix must classify by
            // choice, not blanket-suppress the confirmation surface.
            makeInterpretationEvent({
              id: "evt-approved",
              session_id: sessionFixture.id,
              tool_call_id: "call-set-pipeline",
              user_term: "lead_quality",
              choice: "amended",
              resolved_at: "2026-05-18T10:06:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const confirmations = screen.getAllByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmations).toHaveLength(1);
    expect(confirmations[0].textContent).toMatch(/lead_quality/);
    expect(
      confirmations.map((node) => node.textContent).join(" "),
    ).not.toMatch(/engagement/);
  });

  // ── elspeth-52be5924d7: same term approved twice under one turn ───────────
  //
  // The backend guarantees this shape arrives on DISTINCT tool_call_ids: the
  // staging dedup is scoped per (kind, user_term, affected_node_id), so the
  // same term against two nodes is two pending events, and the
  // uq_interpretation_events_pending_tool_call index forbids two pendings
  // sharing a call id. pipeline_decision terms make it deterministic — every
  // raw-HTML-cleanup review on every field_mapper uses the literal constant
  // 'drop_raw_html_fields'. The old key `${turn.id}:${userTerm}` discarded
  // exactly that differentiator.

  function messagesRaisingTwoToolCalls(
    callIdA: string,
    callIdB: string,
  ): ChatMessage[] {
    const mk = (
      overrides: Partial<ChatMessage> & {
        id: string;
        role: ChatMessage["role"];
      },
    ): ChatMessage =>
      ({
        session_id: sessionFixture.id,
        content: "",
        tool_calls: null,
        created_at: "2026-05-18T10:00:00Z",
        ...overrides,
      }) as ChatMessage;
    return [
      mk({ id: "u1", role: "user", content: "clean both scraped sources" }),
      mk({
        id: "a1",
        role: "assistant",
        tool_calls: [
          {
            id: callIdA,
            type: "function",
            function: {
              name: "request_interpretation_review",
              arguments: "{}",
            },
          },
          {
            id: callIdB,
            type: "function",
            function: {
              name: "request_interpretation_review",
              arguments: "{}",
            },
          },
        ],
      }),
      mk({ id: "a2", role: "assistant", content: "Both cleanups staged." }),
    ];
  }

  function seedSameTermApprovedOnTwoNodes() {
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-clean-main",
              session_id: sessionFixture.id,
              tool_call_id: "call-clean-main",
              affected_node_id: "cleanup_html_main",
              user_term: "drop_raw_html_fields",
              kind: "pipeline_decision",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:05:00Z",
            }),
            makeInterpretationEvent({
              id: "evt-clean-comments",
              session_id: sessionFixture.id,
              tool_call_id: "call-clean-comments",
              affected_node_id: "cleanup_html_comments",
              user_term: "drop_raw_html_fields",
              kind: "pipeline_decision",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:06:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingTwoToolCalls(
        "call-clean-main",
        "call-clean-comments",
      ),
    });
  }

  it("keys same-term approvals under one turn distinctly — no React duplicate-key error", () => {
    // Fails if the anchored key reverts to `${turn.id}:${userTerm}`: React
    // logs "Encountered two children with the same key" through
    // console.error for the two same-term siblings.
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    seedSameTermApprovedOnTwoNodes();
    render(<ChatPanel />);

    expect(
      screen.getAllByTestId("interpretation-review-confirmation"),
    ).toHaveLength(2);
    const duplicateKeyErrors = consoleError.mock.calls.filter((args) =>
      args.some(
        (arg) =>
          typeof arg === "string" &&
          arg.includes("two children with the same key"),
      ),
    );
    expect(duplicateKeyErrors).toHaveLength(0);
  });

  it("labels each anchored confirmation with the node the approval bound to", () => {
    // Two approvals of the same term are two audit-distinct events; without
    // the node on the card the operator cannot tell which approval echoed
    // which review. Fails if InterpretationConfirmation renders only the
    // term.
    seedSameTermApprovedOnTwoNodes();
    render(<ChatPanel />);

    const confirmations = screen.getAllByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmations).toHaveLength(2);
    // flatMap order follows the turn's aggregatedToolCalls order.
    expect(confirmations[0].textContent).toMatch(/cleanup_html_main/);
    expect(confirmations[0].textContent).not.toMatch(/cleanup_html_comments/);
    expect(confirmations[1].textContent).toMatch(/cleanup_html_comments/);
    expect(confirmations[1].textContent).not.toMatch(/cleanup_html_main/);
  });

  it("labels a tail confirmation with its node", () => {
    // The tail path always had unique keys but the same missing node
    // identity. A row with no tool_call_id still carries the node the
    // approval bound to.
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-tail",
              session_id: sessionFixture.id,
              tool_call_id: null,
              affected_node_id: "rater",
              user_term: "lead_quality",
              choice: "amended",
              resolved_at: "2026-05-18T10:05:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });

    render(<ChatPanel />);

    const confirmation = screen.getByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmation.textContent).toMatch(/lead_quality/);
    expect(confirmation.textContent).toMatch(/rater/);
  });


  it.each([
    ["invented_source", "Approved the generated data"],
    ["llm_prompt_template", "Approved the prompts"],
    ["pipeline_decision", "Approved the pipeline decision"],
    ["llm_model_choice", "Approved the model selection"],
    ["source_data_contract", "Approved the input data requirements"],
  ] as const)("uses readable approval copy for %s without exposing its internal term", (kind, label) => {
    useInterpretationEventsStore.setState({
      resolvedBySession: {
        [sessionFixture.id]: [makeInterpretationEvent({
          id: `approval-${kind}`,
          session_id: sessionFixture.id,
          kind,
          user_term: `${kind}:summarize`,
          affected_node_id: "summarize",
          choice: "accepted_as_drafted",
          resolved_at: "2026-05-18T10:06:00Z",
        })],
      },
    });
    useSessionStore.setState({ activeSessionId: sessionFixture.id, sessions: [sessionFixture], messages: [] });
    render(<ChatPanel />);
    const confirmation = screen.getByTestId("interpretation-review-confirmation");
    expect(confirmation.textContent).toContain(`${label} for summarize`);
    expect(confirmation.textContent).not.toContain(`${kind}:summarize`);
    expect(confirmation.querySelector("time")?.getAttribute("datetime")).toBe("2026-05-18T10:06:00Z");
    expect(confirmation.className).not.toContain("message-row--assistant");
  });

  it("routes a resolved backend-auto-surfaced confirmation into the labeled approvals section, off the assistant register", () => {
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-sentinel",
              session_id: sessionFixture.id,
              // The exact previously-uncovered shape: a RESOLVED row whose
              // non-null tool_call_id is absent from every rendered turn.
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}11111111`,
              affected_node_id: "summarize",
              user_term: "llm_prompt_template:summarize",
              kind: "llm_prompt_template",
              llm_draft: "Summarise {{ row.page_content }}",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:06:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const section = screen.getByTestId("interpretation-approvals-section");
    const confirmation = within(section).getByTestId(
      "interpretation-review-confirmation",
    );
    expect(confirmation.textContent).toMatch(/Approved the prompts for summarize/);
    expect(confirmation.textContent).not.toMatch(/llm_prompt_template:summarize/);
    // The register IS the fix: the row must not assert assistant speech.
    expect(confirmation.className).not.toMatch(/message-row--assistant/);
    expect(confirmation.querySelector(".bubble-assistant")).toBeNull();
    // The section still follows the whole stream — honesty comes from the
    // labeled register, not from moving the block.
    const laterUserTurn = screen.getByText("now rate each lead");
    expect(
      laterUserTurn.compareDocumentPosition(section) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("labels the approvals section with the count and lists every unanchorable approval", () => {
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-s1",
              session_id: sessionFixture.id,
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}aa`,
              affected_node_id: "summarize",
              user_term: "llm_prompt_template:summarize",
              kind: "llm_prompt_template",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:06:00Z",
            }),
            makeInterpretationEvent({
              id: "evt-s2",
              session_id: sessionFixture.id,
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}bb`,
              affected_node_id: "rate",
              user_term: "llm_model_choice:rate",
              kind: "llm_model_choice",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:07:00Z",
            }),
            makeInterpretationEvent({
              id: "evt-vanished",
              session_id: sessionFixture.id,
              // Provider-style id absent from rendered turns: the
              // onScreen-miss branch, not the null branch.
              tool_call_id: "call-vanished",
              affected_node_id: "cleaner",
              user_term: "drop_raw_html_fields",
              kind: "pipeline_decision",
              choice: "amended",
              resolved_at: "2026-05-18T10:08:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const section = screen.getByTestId("interpretation-approvals-section");
    expect(
      within(section).getByRole("heading", {
        name: "Interpretation approvals (3)",
      }),
    ).toBeInTheDocument();
    expect(
      within(section).getAllByTestId("interpretation-review-confirmation"),
    ).toHaveLength(3);
  });

  it("keeps the anchored confirmation in the approval register too, and mounts no section when nothing is unanchorable", () => {
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-anchored",
              session_id: sessionFixture.id,
              tool_call_id: "call-set-pipeline",
              user_term: "inline_source_data",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:05:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: messagesRaisingToolCall("call-set-pipeline"),
    });

    render(<ChatPanel />);

    const confirmation = screen.getByTestId(
      "interpretation-review-confirmation",
    );
    // One component, one register — the anchored echo is a system
    // attestation exactly as much as the tail one.
    expect(confirmation.className).toMatch(
      /message-row--interpretation-approval/,
    );
    expect(confirmation.className).not.toMatch(/message-row--assistant/);
    expect(confirmation.querySelector(".bubble-assistant")).toBeNull();
    expect(
      screen.queryByTestId("interpretation-approvals-section"),
    ).toBeNull();
  });

  it("shows unanchored approvals in the approvals section", () => {
    // A review without a matching assistant turn belongs in the approval
    // register, not in a fabricated assistant reply.
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-unanchored",
              session_id: sessionFixture.id,
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}wire-confirm`,
              affected_node_id: "rate_node",
              user_term: "llm_model_choice:rate_node",
              kind: "llm_model_choice",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:09:00Z",
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });

    render(<ChatPanel />);

    const section = screen.getByTestId("interpretation-approvals-section");
    expect(
      within(section).getByTestId("interpretation-review-confirmation")
        .textContent,
    ).toMatch(/rate_node/);
  });

  it("stamps a confirmation with its resolved_at time and omits the stamp when unresolved metadata is absent", () => {
    act(() => {
      useInterpretationEventsStore.setState({
        resolvedBySession: {
          [sessionFixture.id]: [
            makeInterpretationEvent({
              id: "evt-stamped",
              affected_node_id: "summarize",
              session_id: sessionFixture.id,
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}cc`,
              user_term: "llm_prompt_template:summarize",
              kind: "llm_prompt_template",
              choice: "accepted_as_drafted",
              resolved_at: "2026-05-18T10:05:00Z",
            }),
            makeInterpretationEvent({
              id: "evt-unstamped",
              affected_node_id: "rate",
              session_id: sessionFixture.id,
              tool_call_id: `${BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}dd`,
              user_term: "llm_prompt_template:rate",
              kind: "llm_prompt_template",
              choice: "accepted_as_drafted",
              resolved_at: null,
            }),
          ],
        },
      });
    });
    useSessionStore.setState({
      activeSessionId: sessionFixture.id,
      sessions: [sessionFixture],
      messages: [],
    });

    render(<ChatPanel />);

    const confirmations = screen.getAllByTestId(
      "interpretation-review-confirmation",
    );
    const stamped = confirmations.find((node) =>
      /summarize/.test(node.textContent ?? ""),
    );
    const unstamped = confirmations.find((node) =>
      /rate/.test(node.textContent ?? ""),
    );
    expect(stamped).toBeDefined();
    expect(unstamped).toBeDefined();
    const time = stamped!.querySelector("time");
    expect(time).not.toBeNull();
    expect(time!.getAttribute("datetime")).toBe("2026-05-18T10:05:00Z");
    expect(unstamped!.querySelector("time")).toBeNull();
  });
});

describe("ChatPanel chat presentation (ux-review-2026-07-02)", () => {
  const session: Session = {
    id: "session-pres",
    title: "Presentation session",
    created_at: "2026-07-02T10:00:00Z",
    updated_at: "2026-07-02T10:00:00Z",
  };
  const userMessage: ChatMessage = {
    id: "message-pres-1",
    session_id: session.id,
    role: "user",
    content: "Build me a pipeline",
    tool_calls: null,
    created_at: "2026-07-02T10:00:01Z",
  };

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      cancelComposition: vi.fn(),
      isComposing: true,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: session.id,
      sessions: [session],
      messages: [userMessage],
    });
  });

  it("makes the conversation scroll region keyboard-focusable with an accessible name (elspeth-5e43a0c8b2)", () => {
    const { container } = render(<ChatPanel />);

    const log = container.querySelector<HTMLElement>(".chat-panel-messages");
    expect(log).not.toBeNull();
    // Keyboard users must be able to focus the container to arrow-scroll it.
    expect(log?.getAttribute("tabindex")).toBe("0");
    expect(log?.getAttribute("aria-label")).toBe("Conversation");
    // The live-region semantics stay intact alongside focusability.
    expect(log?.getAttribute("role")).toBe("log");
    expect(log?.getAttribute("aria-live")).toBe("polite");
    expect(log?.getAttribute("aria-relevant")).toBe("additions");
  });

  it("mounts the composing indicator OUTSIDE the role=log live region (elspeth-76a0cc485e)", () => {
    // Default beforeEach useComposer mock has isComposing: true, so the
    // indicator is painted.
    const { container } = render(<ChatPanel />);

    const log = container.querySelector<HTMLElement>(".chat-panel-messages");
    const indicator = container.querySelector<HTMLElement>(".composing-indicator");
    expect(log).not.toBeNull();
    expect(indicator).not.toBeNull();
    // Structural fix for the nested-live-region finding: the indicator's
    // role="status" summary must be a SIBLING of the log container, never
    // nested inside it where both live regions could announce the same change.
    expect(log?.contains(indicator)).toBe(false);
    const status = indicator?.querySelector<HTMLElement>('[role="status"]');
    expect(status).not.toBeNull();
    expect(log?.contains(status ?? null)).toBe(false);
    expect(status?.querySelector("button")).toBeNull();
  });

  it("keeps the freeform header to compose-state chrome — authority chip stays, model chip and title do not (elspeth-8fa71e6d15)", () => {
    // The model chip (elspeth-e9f7678de8) relocated to AppHeader; the
    // AuthorityChip is the fact that must stay visible at a glance in the
    // authoring chrome and must NOT ride along in any such move. Load
    // preferences so the chip has an authority to name (it renders nothing
    // until trust_mode is known — absence of chrome, never a fabricated
    // authority claim).
    useSessionStore.setState({
      composerPreferences: {
        session_id: "session-1",
        trust_mode: "auto_commit",
        density_default: "high",
        interpretation_review_disabled: false,
        updated_at: "2026-08-06T00:00:00Z",
      },
    });
    const { container } = render(<ChatPanel />);

    const header = container.querySelector(".chat-panel-header");
    expect(header).not.toBeNull();
    expect(header?.querySelector(".chat-model-chip")).toBeNull();
    expect(header?.querySelector(".chat-panel-header-title")).toBeNull();
    expect(header?.querySelector(".chat-authority-chip")).not.toBeNull();
  });
});

describe("freeform upload session fence (elspeth-341a3e2fc4)", () => {
  // A slow upload started in session A may complete while session B is
  // active. The upload sentence and the blob belong to A: B's composer must
  // never receive them, and A's draft slot must hold the sentence when the
  // user returns (the same slot-targeted retention doctrine as the
  // failed-send restore, elspeth-49b467d91a).
  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    mockedChatInputUpload.blob = null;
    mockedChatInputUpload.requests = [];
    mockedChatInputUpload.completedRequestIds = [];
    mockedChatInputUpload.acceptedRequestIds = [];
    mockedChatInputUpload.settledRequestIds = [];
    mockedChatInputUpload.acceptedFailureRequestIds = [];
    mockedChatInputUpload.immediateRequestSeq = 0;
  });

  function freeformSession(id: string, title: string): Session {
    return {
      id,
      title,
      created_at: "2026-08-09T10:00:00Z",
      updated_at: "2026-08-09T10:00:00Z",
    };
  }

  function freeformBlob(sessionId: string, filename: string): BlobMetadata {
    return {
      id: "00000000-0000-4000-8000-00000000341a",
      session_id: sessionId,
      filename,
      mime_type: "text/csv",
      size_bytes: 16,
      content_hash: "f".repeat(64),
      created_at: "2026-08-09T10:00:00Z",
      created_by: "user",
      source_description: null,
      status: "ready",
      creation_modality: "verbatim",
      created_from_message_id: null,
      creating_model_identifier: null,
      creating_model_version: null,
      creating_provider: null,
      creating_composer_skill_hash: null,
      creating_arguments_hash: null,
    };
  }

  it("routes a slow upload's sentence to the originating session's draft, never the live composer", async () => {
    const upload = deferred<BlobMetadata>();
    useSessionStore.setState({
      activeSessionId: "session-a",
      sessions: [
        freeformSession("session-a", "Session A"),
        freeformSession("session-b", "Session B"),
      ],
      messages: [],
    });
    mockedChatInputUpload.requests = [
      {
        requestId: "upload-fence-1",
        sessionId: "session-a",
        completion: upload.promise,
      },
    ];

    render(<ChatPanel />);
    await act(async () => {
      screen.getByTestId("chat-input-upload").click();
    });

    // The user switches to session B while A's upload is still in flight.
    act(() => {
      useSessionStore.setState({ activeSessionId: "session-b", messages: [] });
    });

    await act(async () => {
      upload.resolve(freeformBlob("session-a", "slow.csv"));
      await upload.promise;
    });

    // The fence must REFUSE the foreign-session completion — the real
    // ChatInput otherwise appends the live (B) text plus the sentence
    // through a stale session-A-bound onChange closure.
    expect(mockedChatInputUpload.acceptedRequestIds).toEqual([]);

    // B's composer must not receive A's upload sentence.
    expect(
      screen.getByTestId("chat-input").getAttribute("data-value"),
    ).not.toContain("I've uploaded");

    // Returning to A finds the sentence waiting in A's draft slot.
    act(() => {
      useSessionStore.setState({ activeSessionId: "session-a", messages: [] });
    });
    expect(
      screen.getByTestId("chat-input").getAttribute("data-value"),
    ).toContain('I\'ve uploaded "slow.csv"');
  });

  it("keeps the live append when the originating session is still active (control)", async () => {
    const upload = deferred<BlobMetadata>();
    useSessionStore.setState({
      activeSessionId: "session-a",
      sessions: [freeformSession("session-a", "Session A")],
      messages: [],
    });
    mockedChatInputUpload.requests = [
      {
        requestId: "upload-fence-2",
        sessionId: "session-a",
        completion: upload.promise,
      },
    ];

    render(<ChatPanel />);
    await act(async () => {
      screen.getByTestId("chat-input-upload").click();
    });
    await act(async () => {
      upload.resolve(freeformBlob("session-a", "fast.csv"));
      await upload.promise;
    });

    expect(
      screen.getByTestId("chat-input").getAttribute("data-value"),
    ).toContain('I\'ve uploaded "fast.csv"');
    expect(
      screen.getByTestId("chat-input").getAttribute("data-value"),
    ).toContain("reference table, or LLM prompt");
  });

  it("suppresses a foreign-session upload failure alert but keeps the owning session's", async () => {
    const upload = deferred<BlobMetadata>();
    useSessionStore.setState({
      activeSessionId: "session-a",
      sessions: [
        freeformSession("session-a", "Session A"),
        freeformSession("session-b", "Session B"),
      ],
      messages: [],
    });
    mockedChatInputUpload.requests = [
      {
        requestId: "upload-fence-3",
        sessionId: "session-a",
        completion: upload.promise,
      },
    ];

    render(<ChatPanel />);
    await act(async () => {
      screen.getByTestId("chat-input-upload").click();
    });
    act(() => {
      useSessionStore.setState({ activeSessionId: "session-b", messages: [] });
    });
    await act(async () => {
      upload.reject(new Error("boom"));
      await upload.promise.catch(() => undefined);
    });

    // The rejection was NOT accepted by the fence: the mock records accepted
    // failures, so an empty list proves the foreign-session alert was fenced.
    expect(mockedChatInputUpload.acceptedFailureRequestIds).toEqual([]);
  });
});

describe("ChatPanel live tool log (elspeth-3c2caf56a7)", () => {
  const session: Session = {
    id: "session-1",
    title: "Composer session",
    created_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T10:00:00Z",
  };

  function userRow(id: string, content: string): ChatMessage {
    return {
      id,
      session_id: "session-1",
      role: "user",
      content,
      tool_calls: null,
      created_at: "2026-08-13T10:00:01Z",
    };
  }

  function midLoopAssistantRow(): ChatMessage {
    return {
      id: "assistant-1",
      session_id: "session-1",
      role: "assistant",
      content: "Checking the schema before submitting.",
      tool_calls: [
        {
          id: "tc-1",
          type: "function",
          function: { name: "get_plugin_schema", arguments: "{}" },
        },
        {
          id: "tc-2",
          type: "function",
          function: { name: "set_pipeline", arguments: "{}" },
        },
        {
          id: "tc-3",
          type: "function",
          function: { name: "set_source", arguments: "{}" },
          outcome: "applied",
        },
      ],
      created_at: "2026-08-13T10:00:02Z",
    };
  }

  function mockComposer(isComposing: boolean) {
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing,
      compositionState: null,
      error: null,
    });
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    mockComposer(true);
  });

  it("surfaces the mid-flight tail turn's tool calls in the indicator while the bubble stays hidden", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [userRow("user-1", "Build the pipeline"), midLoopAssistantRow()],
    });

    render(<ChatPanel />);

    // Atomic-reveal gate: only the user bubble renders; the incomplete tail
    // agent turn stays hidden behind the indicator.
    expect(screen.getAllByTestId("message-bubble")).toHaveLength(1);
    // The live log names each call with outcome-honest prefixes, visible
    // WITHOUT opening the Show-details disclosure: no stamp keeps the
    // conservative lookup label for discovery tools and the neutral Running
    // label for mutating tools — never a fabricated "Applied".
    expect(screen.getByText("Looked up: get_plugin_schema")).toBeInTheDocument();
    expect(screen.getByText("Running: set_pipeline")).toBeInTheDocument();
    expect(screen.queryByText("Applied: set_pipeline")).not.toBeInTheDocument();
    // A server-stamped outcome renders its real verdict.
    expect(screen.getByText("Applied: set_source")).toBeInTheDocument();
  });

  it("keeps the live log outside the aria-live message log and outside the role=status node", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [userRow("user-1", "Build the pipeline"), midLoopAssistantRow()],
    });

    const { container } = render(<ChatPanel />);

    const log = container.querySelector(".composing-tool-log");
    expect(log).not.toBeNull();
    // Not inside any live region: an append-per-poll list inside a polite
    // region would announce every 1.5s tick (WCAG 4.1.3).
    for (
      let node = log!.parentElement;
      node !== null;
      node = node.parentElement
    ) {
      expect(node.getAttribute("aria-live")).toBeNull();
      expect(node.getAttribute("role")).not.toBe("status");
      expect(node.getAttribute("role")).not.toBe("log");
    }
  });

  it("rolls the entries away once the genuine reply lands and composing ends", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [userRow("user-1", "Build the pipeline"), midLoopAssistantRow()],
    });

    const { rerender } = render(<ChatPanel />);
    expect(screen.getByText("Running: set_pipeline")).toBeInTheDocument();

    mockComposer(false);
    act(() => {
      useSessionStore.setState({
        messages: [
          userRow("user-1", "Build the pipeline"),
          midLoopAssistantRow(),
          {
            id: "assistant-2",
            session_id: "session-1",
            role: "assistant",
            content: "Pipeline saved.",
            tool_calls: null,
            created_at: "2026-08-13T10:00:03Z",
          },
        ],
      });
    });
    rerender(<ChatPanel />);

    // Indicator (and its live log) is gone; the completed turn's bubble now
    // renders, where the aggregated calls live in the Tool calls disclosure
    // (MessageBubble's own pinned behaviour — mocked here).
    expect(screen.queryByText("Running: set_pipeline")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("message-bubble")).toHaveLength(2);
  });

  it("clears the entries when a new request's optimistic user row becomes the tail", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [userRow("user-1", "Build the pipeline"), midLoopAssistantRow()],
    });

    render(<ChatPanel />);
    expect(screen.getByText("Running: set_pipeline")).toBeInTheDocument();

    act(() => {
      useSessionStore.setState({
        messages: [
          userRow("user-1", "Build the pipeline"),
          midLoopAssistantRow(),
          { ...userRow("local-123", "Now add an output"), local_status: "pending" },
        ],
      });
    });

    // The tail turn is now the optimistic user row — the old request's calls
    // no longer masquerade as live activity for the new one.
    expect(screen.queryByText("Running: set_pipeline")).not.toBeInTheDocument();
    expect(screen.queryByText("Looked up: get_plugin_schema")).not.toBeInTheDocument();
  });
});

describe("ChatPanel jump-to-latest pill (elspeth-4ad68a3769)", () => {
  const session: Session = {
    id: "session-1",
    title: "Composer session",
    created_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T10:00:00Z",
  };

  function message(id: string, role: "user" | "assistant", content: string): ChatMessage {
    return {
      id,
      session_id: "session-1",
      role,
      content,
      tool_calls: null,
      created_at: "2026-08-13T10:00:01Z",
    };
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
  });

  function renderScrolledUpPanel() {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [session],
      messages: [
        message("user-1", "user", "Build the pipeline"),
        message("assistant-1", "assistant", "Done."),
      ],
    });

    const { container } = render(<ChatPanel />);
    const scroll = container.querySelector<HTMLElement>(".chat-panel-messages");
    expect(scroll).not.toBeNull();
    // jsdom has no layout: stub geometry 560px above the bottom (> 40px).
    let scrollTop = 0;
    Object.defineProperty(scroll!, "scrollHeight", {
      configurable: true,
      get: () => 1000,
    });
    Object.defineProperty(scroll!, "clientHeight", {
      configurable: true,
      get: () => 400,
    });
    Object.defineProperty(scroll!, "scrollTop", {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => {
        scrollTop = v;
      },
    });
    fireEvent.scroll(scroll!);
    return { container, scroll: scroll! };
  }

  it("anchors the pill inside the messages region, outside the scrolling element", () => {
    const { container, scroll } = renderScrolledUpPanel();

    const pill = screen.getByRole("button", { name: "Scroll to bottom" });
    const region = container.querySelector(".chat-panel-messages-region");
    expect(region).not.toBeNull();
    // Direct child of the region — a SIBLING of the scrolling element, so it
    // floats over the messages instead of scrolling away with them, and its
    // bottom anchor is the messages area's edge, not the whole panel's.
    expect(pill.parentElement).toBe(region);
    expect(scroll.parentElement).toBe(region);
    expect(scroll.contains(pill)).toBe(false);
    // Not a sibling of the ChatInput form: the input lives outside the region.
    expect(region!.contains(screen.getByTestId("chat-input"))).toBe(false);
  });

  it("pins the positioning contract the anchoring depends on", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/chat/chat.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\.chat-panel-messages-region\s*\{[^}]*position:\s*relative/s,
    );
    expect(css).toMatch(
      /\.scroll-to-bottom-btn\s*\{[^}]*position:\s*absolute/s,
    );
  });

  it("keeps the pill's jump behaviour through the new structure", () => {
    const { scroll } = renderScrolledUpPanel();

    const pill = screen.getByRole("button", { name: "Scroll to bottom" });
    fireEvent.click(pill);

    // The transcript lands at its end (the helper stubs scrollHeight 1000).
    expect(scroll.scrollTop).toBe(1000);
    expect(
      screen.queryByRole("button", { name: "Scroll to bottom" }),
    ).toBeNull();
  });

  it("scrolls the transcript BY NAME, never by walking ancestors", () => {
    // This assertion is the whole defect, so it is worth stating plainly:
    // scrollIntoView scrolls every scrollable ancestor of its target, and
    // `overflow: hidden` does not make a box unscrollable — it only hides the
    // scrollbar. All three freeform call sites used to fire it at a sentinel
    // inside the transcript, and whenever the docked chrome pushed
    // .chat-panel's content past its own box the call scrolled the PANEL:
    // measured in Chrome at scrollTop 0 -> 130, .chat-panel-header carried to
    // -49, the composer left floating above a void that no re-render could
    // clear because a scroll offset is not React state. Only a reload fixed it.
    //
    // Asserting "the transcript ended up at the bottom" would NOT catch that —
    // the old code satisfied it too, on its way past. The observable that
    // separates a correct scroll from the defect is the INSTRUMENT: a scroller
    // named directly cannot move anything above it. So this pins zero
    // scrollIntoView calls on the freeform path, and it is the assertion that
    // fails if anyone reaches for the convenient API again.
    const walkSpy = vi.fn();
    Element.prototype.scrollIntoView = walkSpy;

    const { scroll } = renderScrolledUpPanel();
    fireEvent.click(screen.getByRole("button", { name: "Scroll to bottom" }));

    expect(walkSpy).not.toHaveBeenCalled();
    expect(scroll.scrollTop).toBe(1000);
  });

  it("pins the panel as a clip box and the dock as the yielding claimant", () => {
    // jsdom computes no layout, so the two rules that make the fix structural
    // are unobservable to a DOM test — same stylesheet-reading idiom as the
    // positioning-contract test above, for the same reason.
    // Comments are stripped first: both rules below CARRY a comment that
    // quotes the declaration it replaced, so a naive match reads the prose as
    // the code and the negative assertion below inverts.
    const css = readFileSync(
      join(process.cwd(), "src/components/chat/chat.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    // EVERY body for the selector, not the first. A regex that stops at the
    // first match would miss a later override — inside an @media block, say —
    // which is precisely the regression these assertions exist to catch.
    const ruleBodies = (selector: string): string[] => {
      const bodies: string[] = [];
      const pattern = new RegExp(`(^|[\\s,}])${selector}\\s*\\{([^{}]*)\\}`, "gm");
      for (const match of css.matchAll(pattern)) bodies.push(match[2]);
      return bodies;
    };
    const declaration = (bodies: string[], property: string): string | null => {
      // The LAST declaration across all matching rules is the one that wins,
      // which also lets a `hidden`-then-`clip` progressive-enhancement pair be
      // written correctly in future without failing this test.
      // The value class must admit functional notation — `min(160px, 30%)`
      // is a value this stylesheet actually ships, and a narrower class
      // silently skips it and reports an EARLIER declaration as the winner.
      const pattern = new RegExp(`(?:^|[;{\\s])${property}:\\s*([^;{}]+?);`, "g");
      let winner: string | null = null;
      for (const body of bodies) {
        for (const match of body.matchAll(pattern)) winner = match[1].trim();
      }
      return winner;
    };

    // `clip` is load-bearing, not a synonym for `hidden`: a clip box is not a
    // scroll container, so no ancestor walk — scrollIntoView, focus(),
    // find-in-page, an AT caret — can give this panel a scroll offset at all.
    // Reverting this one word restores the defect even with the call sites
    // fixed, because the panel becomes scrollable again.
    const panelRules = ruleBodies("\\.chat-panel");
    expect(panelRules.length).toBeGreaterThan(0);
    expect(declaration(panelRules, "overflow")).toBe("clip");

    // The dock absorbs the deficit so .chat-input never does. overflow-y:auto
    // is the mechanism — it zeroes the dock's automatic minimum size AND
    // keeps every docked surface reachable while the box is squeezed. Drop it
    // and the composer is pushed through the panel's bottom edge again
    // (measured 944px below it at a short panel, clipped away entirely).
    const dock = ruleBodies("\\.chat-panel-dock");
    expect(declaration(dock, "flex")).toBe("0 1 auto");
    expect(declaration(dock, "overflow-y")).toBe("auto");
    // Scroll chaining out of the dock into the transcript, same treatment as
    // .chat-panel > .ack-stack.
    expect(declaration(dock, "overscroll-behavior")).toBe("contain");

    // The composer never yields — DECLARED. Without this, .chat-input is
    // shrinkable (flex-shrink defaults to 1) and survives only on
    // min-height:auto freezing it at its content minimum. Adding this
    // codebase's own `min-height: 0` idiom to that rule collapsed it from
    // 169px to 54px with every other test still green.
    expect(declaration(ruleBodies("\\.chat-input"), "flex-shrink")).toBe("0");

    // The transcript never reaches zero. `flex: 1` carries a zero basis, so
    // this region contributes NOTHING to shrinking and is driven to 0 before
    // the dock yields a pixel — leaving the operator approving a pipeline
    // mutation with no visible conversation. The min() clamp is load-bearing:
    // a bare 160px floor overflowed the panel at 367px.
    const regionFloor = declaration(
      ruleBodies("\\.chat-panel-messages-region"),
      "min-height",
    );
    expect(regionFloor).toMatch(/^min\(/);
  });

  it("docks every optional surface, and never the composer, inside the dock", () => {
    // The dock only settles the budget if the composer is OUTSIDE it: a
    // .chat-input that shrank with the dock would be squeezed away instead of
    // pushed away — the same operator-facing loss by a different route.
    const { container } = renderScrolledUpPanel();

    const panel = container.querySelector<HTMLElement>("#chat-main");
    const dock = container.querySelector<HTMLElement>(".chat-panel-dock");
    expect(dock).not.toBeNull();
    expect(dock!.parentElement).toBe(panel);

    const input = screen.getByTestId("chat-input");
    expect(dock!.contains(input)).toBe(false);
    // …and it renders BELOW the dock. Document order rather than
    // lastElementChild: the ChatInput mock in this file returns a fragment, so
    // the panel's last element child is an artefact of the mock's shape.
    //
    // Order is NOT why the dock is what gets squeezed — flex shrinkage is
    // simultaneous and proportional to flex-shrink x flex-basis, then
    // redistributed as items freeze at their clamps. The dock yields because
    // its floor is 0 (it is a scroll container) while .chat-input's is
    // declared flex-shrink: 0. Both facts are asserted in the stylesheet test.
    expect(
      dock!.compareDocumentPosition(input) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // The optional surfaces are inside it. The composer-progress card is the
    // one that grows without bound (its details default OPEN on a terminal
    // phase), so it is the load-bearing member of the group.
    const indicator = panel!.querySelector(".composing-indicator");
    if (indicator !== null) expect(dock!.contains(indicator)).toBe(true);
  });
});

// ── Decision panel wiring (elspeth-cb0d4b8dba) ───────────────────────────────
//
// The live 94f6f00c shape: a green build whose completion the advisor gate
// withheld, plus the validator's S1 suggestion on the composition. Before
// this panel the Compose view carried no affordance for it at all.
describe("ChatPanel decision panel (elspeth-cb0d4b8dba)", () => {
  const S1 = {
    component: "pipeline",
    message:
      "Consider adding error routing to a retention output — failed rows are currently discarded rather than kept for review.",
    severity: "low",
  };

  function withheldValidation(note: string | null = null) {
    return {
      is_valid: true,
      checks: [],
      errors: [],
      warnings: [],
      readiness: {
        authoring_valid: true,
        execution_ready: true,
        completion_ready: false,
        blockers: [
          {
            code: "advisor_signoff_blocked",
            component_id: "pipeline",
            suggestion: null,
            note,
            component_type: "pipeline",
            detail:
              "Completion advisory review did not clear after the available attempts.",
          },
        ],
      },
    };
  }

  beforeEach(() => {
    vi.resetAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    resetStore(useInlineSourceStore);
    resetStore(useExecutionStore);
    resetStore(useInterpretationEventsStore);
    (useComposer as ReturnType<typeof vi.fn>).mockReturnValue({
      sendMessage: vi.fn(),
      retryMessage: vi.fn(),
      isComposing: false,
      compositionState: null,
      error: null,
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [],
      compositionState: makeComposition(7, { validation_suggestions: [S1] }),
      composeTimeoutReady: true,
      composerTimeoutUnavailable: false,
    });
  });

  it("renders no panel while validation is green and nothing is pending", () => {
    useExecutionStore.setState({ validationResult: { ...withheldValidation(), readiness: { authoring_valid: true, execution_ready: true, completion_ready: true, blockers: [] } } });
    render(<ChatPanel />);
    expect(screen.queryByRole("region", { name: /awaiting your decision/i })).toBeNull();
  });

  it("surfaces the withheld completion and the suggestion above the input, with Apply sending the pinned prompt", () => {
    useExecutionStore.setState({ validationResult: withheldValidation() });
    render(<ChatPanel />);

    const panel = screen.getByRole("region", { name: "Awaiting your decision (2)" });
    expect(
      within(panel).getByText("Share inspect link is blocked. Run pipeline is still available."),
    ).toBeInTheDocument();
    expect(
      within(panel).getByText(
        "Completion advisory review did not clear after the available attempts.",
      ),
    ).toBeInTheDocument();

    // Anchored above the input, not inside the transcript log.
    const log = screen.getByRole("log", { name: "Conversation" });
    expect(log.contains(panel)).toBe(false);

    fireEvent.click(within(panel).getByRole("button", { name: /^Apply optional suggestion/ }));
    expect(useComposer().sendMessage).toHaveBeenCalledExactlyOnceWith(
      "Please apply this suggestion to the pipeline:\n\n**pipeline:** Consider adding error routing to a retention output — failed rows are currently discarded rather than kept for review.",
    );
  });

  it("Ask the composer about a blocker drafts a question into the input and sends nothing (D4)", async () => {
    useExecutionStore.setState({ validationResult: withheldValidation("Choose per-branch sinks or best_effort.") });
    render(<ChatPanel />);
    const panel = screen.getByRole("region", { name: "Awaiting your decision (2)" });
    const ask = within(panel).getByRole("button", { name: /^Ask the composer about this: Completion advisory review/ });

    fireEvent.click(ask);

    // ChatInput is mocked here; it mirrors the controlled value it is given.
    const draft = (): string => screen.getByTestId("chat-input").getAttribute("data-value") ?? "";
    await waitFor(() => expect(draft()).toContain(
      "> Completion advisory review did not clear after the available attempts.",
    ));
    expect(draft()).toContain("Unverified advisor note (untrusted evidence)");
    expect(draft()).toContain("> Choose per-branch sinks or best_effort.");
    expect(draft()).toMatch(/What does it mean, and what are my options\?$/);
    expect(useComposer().sendMessage).not.toHaveBeenCalled();
    // The draft is now text the click would replace: Ask closes, visibly.
    expect(ask).toBeDisabled();
    expect(within(panel).getByText("Send or clear your draft before asking about a blocker.")).toBeInTheDocument();
  });

  it("Open checks requests the Checks artifact tab for the active session", () => {
    useExecutionStore.setState({ validationResult: withheldValidation() });
    const seen: unknown[] = [];
    const listener = (event: Event): void => {
      seen.push((event as CustomEvent).detail);
    };
    window.addEventListener("elspeth:request-artifact-view", listener);
    try {
      render(<ChatPanel />);
      fireEvent.click(screen.getByRole("button", { name: "Open checks" }));
    } finally {
      window.removeEventListener("elspeth:request-artifact-view", listener);
    }
    expect(seen).toEqual([{ tab: "checks", focusMode: false, sessionId: "session-1" }]);
  });

  it("holds Apply closed until the compose wall clock lands", () => {
    useExecutionStore.setState({ validationResult: withheldValidation() });
    useSessionStore.setState({ composeTimeoutReady: false });
    render(<ChatPanel />);
    const panel = screen.getByRole("region", { name: "Awaiting your decision (2)" });
    expect(within(panel).getByRole("button", { name: /^Apply optional suggestion/ })).toBeDisabled();
    expect(within(panel).getByRole("status")).toHaveTextContent(COMPOSE_CONNECTING_MESSAGE);
  });


  it("keeps Apply available in freeform authoring", () => {
    useExecutionStore.setState({ validationResult: withheldValidation() });
    render(<ChatPanel />);
    const apply = within(screen.getByTestId("decision-panel")).getByRole("button", { name: /^Apply optional suggestion/ });
    expect(apply).toBeEnabled();
    fireEvent.click(apply);
    expect(useComposer().sendMessage).toHaveBeenCalledOnce();
  });


  it("owns pending interpretation actions and announces arrivals once", async () => {
    render(<ChatPanel />);
    const event: InterpretationEvent = {
      id: "decision-review",
      session_id: "session-1",
      composition_state_id: "state-1",
      affected_node_id: null,
      tool_call_id: "call-review",
      user_term: "cool",
      kind: "vague_term",
      llm_draft: "trendy",
      accepted_value: null,
      choice: "pending",
      interpretation_source: "user_approved",
      created_at: "2026-09-19T00:00:00Z",
      resolved_at: null,
      actor: "system:composer",
      model_identifier: "test-model",
      model_version: "test-model",
      provider: "test-provider",
      composer_skill_hash: "0".repeat(64),
      arguments_hash: null,
      hash_domain_version: null,
      runtime_model_identifier_at_resolve: null,
      runtime_model_version_at_resolve: null,
      approved_prompt_artifact_hash: null,
    };
    act(() => useInterpretationEventsStore.getState().addPendingEvent("session-1", event));
    expect(screen.queryByTestId("acknowledgement-live-region")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("decision-panel-live-region")).toHaveTextContent("1 item needs your decision"));
    const panel = screen.getByTestId("decision-panel");
    expect(within(panel).getByTestId("acknowledgement-stack")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show interpretation review: cool" })).not.toBeInTheDocument();
  });

  it("renders proposals as native rows without a nested Pending changes region", () => {
    useExecutionStore.setState({ validationResult: withheldValidation() });
    useSessionStore.setState({
      compositionProposals: [
        {
          id: "proposal-1",
          session_id: "session-1",
          tool_call_id: "call-1",
          tool_name: "patch_node_options",
          status: "pending",
          summary: "Change one option on colour_questions.",
          rationale: "Requested by the current composer turn.",
          affects: ["nodes"],
          arguments_redacted_json: {},
          base_state_id: null,
          committed_state_id: null,
          audit_event_id: null,
          created_at: "2026-09-13T11:16:03Z",
          updated_at: "2026-09-13T11:16:03Z",
        },
      ],
    });
    render(<ChatPanel />);
    const panel = screen.getByRole("region", { name: "Awaiting your decision (3)" });
    expect(within(panel).getByRole("button", { name: "Accept proposal: Change one option on colour_questions." })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Pending changes (1)" })).not.toBeInTheDocument();
  });
});
