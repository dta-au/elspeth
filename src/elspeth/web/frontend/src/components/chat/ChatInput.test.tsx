// ============================================================================
// ChatInput — listener-stabilisation regression coverage.
//
// Pins the contract that the PREFILL_CHAT_INPUT_EVENT listener must (a) be
// registered exactly once for the lifetime of the component (not re-registered
// on every parent re-render in controlled mode), and (b) always resolve to the
// latest setText / onChange handler — i.e. the ref-trampoline pattern at
// ChatInput.tsx:51-52 is load-bearing.  A behavioural test that only fired one
// event would still pass if a future refactor reverted to closing over setText
// directly; this test fires the event AFTER a parent re-render to catch that.
// ============================================================================

import { readFileSync } from "node:fs";

import { useRef, useState, type RefObject } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatInput } from "./ChatInput";
import {
  ACKNOWLEDGEMENT_ACCEPT_LABEL,
  ACKNOWLEDGEMENT_AMEND_LABEL,
  ACKNOWLEDGEMENT_APPROVE_LABEL,
  ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
} from "./acknowledgementLabels";
import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import { PREFILL_CHAT_INPUT_EVENT } from "@/components/catalog/PluginCard";
import type { ChatMessage, CompositionState } from "@/types";
import type { BlobMetadata } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";

describe("ChatInput — controlled-mode prefill listener", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  function ControlledHarness() {
    const [value, setValue] = useState("");
    const [renderTick, setRenderTick] = useState(0);
    const inputRef = useRef<HTMLTextAreaElement>(
      null,
    ) as RefObject<HTMLTextAreaElement>;
    // CRITICAL: onChange closes over `renderTick`.  This is the discriminator
    // that turns the ref-trampoline test from a tautology into a real test.
    // - With the trampoline, setTextRef.current points at the LATEST onChange,
    //   which captures the LATEST renderTick.  After 3 rerenders, prefill
    //   writes "${detail}:3".
    // - Without the trampoline, the listener closes over the FIRST onChange,
    //   which captures renderTick=0.  Prefill writes "${detail}:0".
    // The suffix asymmetry is what proves the trampoline is load-bearing.
    return (
      <div>
        <button
          type="button"
          data-testid="force-rerender"
          onClick={() => setRenderTick((n) => n + 1)}
        >
          rerender {renderTick}
        </button>
        <ChatInput
          onSend={vi.fn()}
          disabled={false}
          inputRef={inputRef}
          value={value}
          onChange={(next) => setValue(`${next}:${renderTick}`)}
        />
      </div>
    );
  }

  it("populates the textarea when PREFILL_CHAT_INPUT_EVENT fires", async () => {
    render(<ControlledHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("");

    act(() => {
      window.dispatchEvent(
        new CustomEvent(PREFILL_CHAT_INPUT_EVENT, {
          detail: "Add csv as the source",
        }),
      );
    });

    // renderTick=0 at this point — harness writes "${detail}:0".
    expect(textarea.value).toBe("Add csv as the source:0");
  });

  it("uses the LATEST setText after parent re-renders (ref-trampoline must be load-bearing)", async () => {
    // Regression: a previous bug closed over setText directly in the effect.
    // In controlled mode, setText identity changes on every parent render.
    // Without the ref trampoline (ChatInput.tsx:51-52), the listener would
    // hold a stale closure pointing to the FIRST onChange — which captures
    // renderTick=0.  After 3 rerenders, prefill should write "${detail}:3"
    // (latest tick) if the trampoline works.  If the listener has stale
    // closure, it writes "${detail}:0" and this test fails.
    const user = userEvent.setup();
    render(<ControlledHarness />);

    await user.click(screen.getByTestId("force-rerender"));
    await user.click(screen.getByTestId("force-rerender"));
    await user.click(screen.getByTestId("force-rerender"));

    act(() => {
      window.dispatchEvent(
        new CustomEvent(PREFILL_CHAT_INPUT_EVENT, { detail: "after rerenders" }),
      );
    });

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    // With the trampoline: latest onChange ran, captured renderTick=3.
    // Without the trampoline: stale onChange ran, captured renderTick=0.
    expect(textarea.value).toBe("after rerenders:3");
  });

  it("registers the listener exactly once across re-renders", async () => {
    // Verify the [] dep array on the prefill effect: addEventListener must
    // not fire on every render.  We spy on window.addEventListener and count
    // PREFILL_CHAT_INPUT_EVENT registrations.
    const addSpy = vi.spyOn(window, "addEventListener");
    const user = userEvent.setup();
    render(<ControlledHarness />);

    const initial = addSpy.mock.calls.filter(
      ([type]) => type === PREFILL_CHAT_INPUT_EVENT,
    ).length;
    expect(initial).toBe(1);

    await user.click(screen.getByTestId("force-rerender"));
    await user.click(screen.getByTestId("force-rerender"));

    const after = addSpy.mock.calls.filter(
      ([type]) => type === PREFILL_CHAT_INPUT_EVENT,
    ).length;
    expect(after).toBe(1);

    addSpy.mockRestore();
  });

  it("throws TypeError on non-string event detail (CLAUDE.md trust-tier: internal contract violations crash)", () => {
    // WHATWG DOM spec: event listener errors do NOT propagate through
    // dispatchEvent — the caller continues; the error is reported via
    // window.onerror / 'error' event.  Capture that report to prove the
    // crash actually fires and is loud (DevTools-visible) rather than
    // silently caught.
    const errorEvents: ErrorEvent[] = [];
    const errorListener = (e: ErrorEvent) => {
      errorEvents.push(e);
      e.preventDefault(); // suppress jsdom's "unhandled error" stderr noise
    };
    window.addEventListener("error", errorListener);

    render(<ControlledHarness />);

    // Dispatch the malformed event — listener throws synchronously.
    window.dispatchEvent(
      new CustomEvent(PREFILL_CHAT_INPUT_EVENT, {
        detail: { not: "a string" } as unknown as string,
      }),
    );

    // The TypeError must have been reported as an unhandled error.
    expect(errorEvents).toHaveLength(1);
    expect(errorEvents[0].error).toBeInstanceOf(TypeError);
    expect(errorEvents[0].error.message).toContain("PREFILL_CHAT_INPUT_EVENT");

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    // No silent state mutation: the bogus value must not have been written.
    expect(textarea.value).toBe("");

    window.removeEventListener("error", errorListener);
  });
});

// ============================================================================
// ChatInput — empty-state placeholder (Phase 5a Task 1).
//
// Primes the user to type data directly into the chat when the session is
// fresh (no messages, no composition state).  Reverts to the canonical
// "Describe the pipeline you want to build..." wording the moment either
// signal flips.  An explicit `placeholder` prop continues to win — Phase A
// slice 4 (guided-mode per-step nudge) depends on that override semantics.
// ============================================================================

describe("ChatInput empty-state placeholder", () => {
  const DATA_PRIMING =
    "Describe your pipeline, paste a URL, or type a few rows of data to start...";
  const STANDARD = "Describe the pipeline you want to build...";

  function StandaloneHarness(props: { placeholder?: string }) {
    const inputRef = useRef<HTMLTextAreaElement>(
      null,
    ) as RefObject<HTMLTextAreaElement>;
    return (
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={inputRef}
        placeholder={props.placeholder}
      />
    );
  }

  function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
    return {
      id: overrides.id ?? "m1",
      session_id: overrides.session_id ?? "s1",
      role: overrides.role ?? "user",
      content: overrides.content ?? "hello",
      tool_calls: overrides.tool_calls ?? null,
      created_at: overrides.created_at ?? "2026-05-18T00:00:00Z",
      ...overrides,
    };
  }

  function makeCompositionState(version: number): CompositionState {
    return {
      id: "comp-1",
      ...compositionStateAuthorityFields,
      version,
      sources: {},
      nodes: [],
      edges: [],
      outputs: [],
      metadata: {} as CompositionState["metadata"],
    };
  }

  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  it("shows the data-priming placeholder when the session has no messages and no composition state", () => {
    // arrange: fresh store — messages=[], compositionState=null (version=0)
    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(DATA_PRIMING);
  });

  it("reverts to the standard placeholder once the user has sent a message", () => {
    useSessionStore.setState({ messages: [makeMessage({ role: "user" })] });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(STANDARD);
  });

  it("reverts to the standard placeholder once a composition state exists", () => {
    useSessionStore.setState({ compositionState: makeCompositionState(1) });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(STANDARD);
  });

  it("respects an explicit `placeholder` prop override even in empty state", () => {
    // Empty state — store untouched — but the prop must still win.
    // This pins the Phase A slice 4 contract: guided-mode per-step nudges
    // override the empty-state default.
    render(<StandaloneHarness placeholder="custom" />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe("custom");
  });
});

describe("ChatInput composing cancel", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  it("shows a stop button while composing and calls onCancel", async () => {
    const user = userEvent.setup();
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;
    const onCancel = vi.fn();

    render(
      <ChatInput
        onSend={vi.fn()}
        disabled={true}
        onCancel={onCancel}
        inputRef={inputRef}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Stop composing" }));

    expect(onCancel).toHaveBeenCalledOnce();
  });
});

describe("ChatInput upload identity", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  it("reports the exact uploaded blob metadata to the owning guided turn", async () => {
    const sessionId = "00000000-0000-4000-8000-000000000811";
    const uploaded = {
      id: "00000000-0000-4000-8000-000000000812",
      session_id: sessionId,
      filename: "intended.csv",
      mime_type: "text/csv",
      size_bytes: 12,
      content_hash: "f".repeat(64),
      created_at: "2026-07-26T09:00:00Z",
      created_by: "user" as const,
      source_description: null,
      status: "ready" as const,
      creation_modality: "verbatim" as const,
      created_from_message_id: null,
      creating_model_identifier: null,
      creating_model_version: null,
      creating_provider: null,
      creating_composer_skill_hash: null,
      creating_arguments_hash: null,
    };
    useSessionStore.setState({ activeSessionId: sessionId });
    useBlobStore.setState({ uploadBlob: vi.fn().mockResolvedValue(uploaded) });
    const onBlobUploaded = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={{ current: null } as RefObject<HTMLTextAreaElement>}
        onBlobUploaded={onBlobUploaded}
      />,
    );
    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();

    await userEvent.upload(fileInput!, new File(["id\n1\n"], "intended.csv", { type: "text/csv" }));

    await waitFor(() => expect(onBlobUploaded).toHaveBeenCalledWith(uploaded));
  });

  it("does not publish or append a completion rejected by its owner fence", async () => {
    let resolveUpload: (blob: BlobMetadata) => void = () => undefined;
    const uploadPromise = new Promise<BlobMetadata>((resolve) => {
      resolveUpload = resolve;
    });
    const sessionId = "00000000-0000-4000-8000-000000000821";
    const uploaded: BlobMetadata = {
      id: "00000000-0000-4000-8000-000000000822",
      session_id: sessionId,
      filename: "stale.csv",
      mime_type: "text/csv",
      size_bytes: 12,
      content_hash: "f".repeat(64),
      created_at: "2026-07-26T09:00:00Z",
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
    const onBlobUploaded = vi.fn();
    const onBlobUploadStarted = vi.fn();
    const onBlobUploadCompleted = vi.fn().mockReturnValue(false);
    const onBlobUploadSettled = vi.fn();
    useSessionStore.setState({ activeSessionId: sessionId });
    useBlobStore.setState({ uploadBlob: vi.fn().mockReturnValue(uploadPromise) });

    function ControlledUploadHarness() {
      const [value, setValue] = useState("");
      return (
        <ChatInput
          onSend={vi.fn()}
          disabled={false}
          inputRef={{ current: null } as RefObject<HTMLTextAreaElement>}
          value={value}
          onChange={setValue}
          onBlobUploaded={onBlobUploaded}
          onBlobUploadStarted={onBlobUploadStarted}
          onBlobUploadCompleted={onBlobUploadCompleted}
          onBlobUploadSettled={onBlobUploadSettled}
        />
      );
    }

    render(<ControlledUploadHarness />);
    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();
    await userEvent.upload(
      fileInput!,
      new File(["id\n1\n"], "stale.csv", { type: "text/csv" }),
    );
    expect(onBlobUploadStarted).toHaveBeenCalledOnce();

    await act(async () => {
      resolveUpload(uploaded);
      await uploadPromise;
    });
    await waitFor(() => expect(onBlobUploadSettled).toHaveBeenCalledOnce());

    expect(onBlobUploadCompleted).toHaveBeenCalledOnce();
    expect(onBlobUploaded).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/message input/i)).toHaveValue("");
  });

  it("does not show a late upload failure rejected by its owner fence", async () => {
    let rejectUpload: (reason?: unknown) => void = () => undefined;
    const uploadPromise = new Promise<BlobMetadata>((_resolve, reject) => {
      rejectUpload = reject;
    });
    const sessionId = "00000000-0000-4000-8000-000000000831";
    const onBlobUploadRejected = vi.fn().mockReturnValue(false);
    const onBlobUploadSettled = vi.fn();
    useSessionStore.setState({ activeSessionId: sessionId });
    useBlobStore.setState({ uploadBlob: vi.fn().mockReturnValue(uploadPromise) });

    render(
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={{ current: null } as RefObject<HTMLTextAreaElement>}
        onBlobUploadRejected={onBlobUploadRejected}
        onBlobUploadSettled={onBlobUploadSettled}
      />,
    );
    const fileInput = document.querySelector<HTMLInputElement>(
      'input[type="file"]',
    );
    expect(fileInput).not.toBeNull();
    await userEvent.upload(
      fileInput!,
      new File(["id\n1\n"], "stale.csv", { type: "text/csv" }),
    );

    await act(async () => {
      rejectUpload(new Error("late upload failure"));
    });
    await waitFor(() => expect(onBlobUploadSettled).toHaveBeenCalledOnce());

    expect(onBlobUploadRejected).toHaveBeenCalledOnce();
    expect(
      screen.queryByText("Upload failed. Check the file manager for details."),
    ).not.toBeInTheDocument();
  });

  it("shows a current upload failure accepted by its owner fence", async () => {
    const uploadError = new Error("current upload failure");
    const onBlobUploadRejected = vi.fn().mockReturnValue(true);
    const onBlobUploadSettled = vi.fn();
    useSessionStore.setState({ activeSessionId: "session-current" });
    useBlobStore.setState({ uploadBlob: vi.fn().mockRejectedValue(uploadError) });

    render(
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={{ current: null } as RefObject<HTMLTextAreaElement>}
        onBlobUploadRejected={onBlobUploadRejected}
        onBlobUploadSettled={onBlobUploadSettled}
      />,
    );
    const fileInput = document.querySelector<HTMLInputElement>(
      'input[type="file"]',
    );
    expect(fileInput).not.toBeNull();
    await userEvent.upload(
      fileInput!,
      new File(["id\n1\n"], "current.csv", { type: "text/csv" }),
    );

    await waitFor(() => expect(onBlobUploadSettled).toHaveBeenCalledOnce());
    expect(onBlobUploadRejected).toHaveBeenCalledOnce();
    expect(
      screen.getByText("Upload failed. Check the file manager for details."),
    ).toBeInTheDocument();
  });
});

describe("ChatInput max length", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  it("passes the configured maxLength to the textarea", () => {
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;

    render(
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={inputRef}
        maxLength={4096}
      />,
    );

    expect(screen.getByLabelText(/message input/i)).toHaveAttribute(
      "maxlength",
      "4096",
    );
  });
});

describe("ChatInput mobile density CSS", () => {
  const chatCss = readFileSync("src/components/chat/chat.css", "utf8");

  it("bounds textarea growth against the desktop dynamic viewport", () => {
    expect(chatCss).toMatch(
      /\.chat-input-textarea\s*\{[^}]*max-height:\s*min\(28dvh, 240px\);[^}]*overflow-y:\s*auto;/s,
    );
  });

  // chat.css has FOUR separate `@media (max-width: 760px)` blocks (inline
  // run-results, this composer-density block, inline-source-fallback, and
  // responsive CSS blocks). A plain indexOf lands on whichever comes first in
  // the file — not necessarily the composer-density block these tests mean
  // to pin — and then over-reads to the next top-level comment, which
  // happens to swallow the intended block too, so the assertions passed by
  // accident (elspeth-05d5caa717). Bound each candidate block to its OWN
  // closing brace (mirrors Layout.test.tsx's regex idiom: nested rules
  // close on an indented `}`, the media block itself closes on an
  // unindented `\n}`) and disambiguate among same-breakpoint blocks by a
  // selector marker unique to the one under test.
  function mediaBlock(maxWidth: number, marker: string): string {
    const pattern = new RegExp(
      `@media \\(max-width: ${maxWidth}px\\)\\s*\\{([\\s\\S]*?)\\n\\}`,
      "g",
    );
    for (const match of chatCss.matchAll(pattern)) {
      if (match[1].includes(marker)) {
        return match[1];
      }
    }
    throw new Error(
      `No max-width ${maxWidth}px media block in chat.css contains "${marker}"`,
    );
  }

  it("compacts the composer chrome on phones without shrinking the textarea out of view", () => {
    const block = mediaBlock(760, ".chat-input-textarea");

    expect(block).toContain(".chat-input {");
    expect(block).toContain("padding: var(--space-xs) var(--space-sm)");
    expect(block).toContain("min-height: 54px");
    expect(block).toContain("max-height: 28vh");
    expect(block).toContain(".chat-input-icon-btn");
    expect(block).toContain("min-width: 44px");
    expect(block).toContain("min-height: var(--size-control)");
    expect(block).toContain(".chat-input-send-btn");
  });

  it("gives composer controls overflow-safe labels on narrow screens", () => {
    const block = mediaBlock(760, ".chat-input-textarea");

    expect(block).toContain("min-width: 0");
    expect(block).toContain("overflow-wrap: anywhere");
  });
});

// ============================================================================
// ChatInput — pending-interpretation placeholder cue (Phase 5b Task 8).
//
// When an InterpretationReviewTurn widget is awaiting the user's decision and
// the underlying interpretation event has a non-null `user_term`, the
// chat-input placeholder briefly cues the user toward the widget above.
// Auto-baked rows (interpretation_source = auto_interpreted_*) have
// user_term=null and MUST NOT trigger the cue — they have no term to echo.
// The cue sits between the explicit `placeholder` prop (still wins) and
// the empty-state / standard placeholders (both lose to the cue when present).
// ============================================================================

describe("ChatInput pending-interpretation placeholder cue", () => {
  const EMPTY_STATE =
    "Describe your pipeline, paste a URL, or type a few rows of data to start...";
  const ACTIVE_SESSION_ID = "sess-1";

  function StandaloneHarness(props: { placeholder?: string }) {
    const inputRef = useRef<HTMLTextAreaElement>(
      null,
    ) as RefObject<HTMLTextAreaElement>;
    return (
      <ChatInput
        onSend={vi.fn()}
        disabled={false}
        inputRef={inputRef}
        placeholder={props.placeholder}
      />
    );
  }

  function makePendingEvent(
    overrides: Partial<InterpretationEvent> = {},
  ): InterpretationEvent {
    // Spread overrides last so explicit `null` (e.g. user_term=null for
    // auto-baked rows) overrides the defaults.  `??` short-circuits on
    // null/undefined and would silently drop intentional null overrides
    // — spread semantics keep them.
    const defaults: InterpretationEvent = {
      id: "evt-1",
      session_id: ACTIVE_SESSION_ID,
      composition_state_id: "state-1",
      affected_node_id: "node-1",
      tool_call_id: "tool-1",
      user_term: "cool",
      kind: "vague_term",
      llm_draft: "interesting and engaging",
      accepted_value: null,
      choice: "pending",
      created_at: "2026-05-18T00:00:00Z",
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
      resolved_prompt_template_hash: null,
    };
    return { ...defaults, ...overrides };
  }

  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  it("shows the interpretation-review cue when a pending event with a user_term exists for the active session", () => {
    // arrange: active session + one pending event with user_term="cool"
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({ user_term: "cool" });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    // Directionally neutral (elspeth-eba8820005): the review card sits in a
    // different column in the guided workspace, so the cue names the card
    // rather than pointing "above".
    expect(textarea.placeholder).toBe(
      'Reviewing your interpretation of "cool" — pick Acknowledge or Change… on the review card to continue.',
    );
  });

  it("names the card's exact button labels in the cue (anti-drift, elspeth-0a9f77dd75)", () => {
    // Truth test via the shared constants: the cue must name the buttons the
    // card actually renders, and the retired inline-review vocabulary must
    // not resurface.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({ user_term: "cool" });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toContain(ACKNOWLEDGEMENT_ACCEPT_LABEL);
    expect(textarea.placeholder).toContain(ACKNOWLEDGEMENT_AMEND_LABEL);
    expect(textarea.placeholder).not.toContain("Use mine");
    expect(textarea.placeholder).not.toContain("Change it");
  });

  it("prompt-template cue names View prompt then Approve, never the vague-term buttons (kind-aware, ux-review 2026-08-13)", () => {
    // Prompt-template cards render "View prompt" + "Approve" — neither
    // "Acknowledge" nor "Change…" exists on them, so the cue must not name
    // absent controls.  The machine-facing user_term
    // ("llm_prompt_template:<node id>") is deliberately not echoed.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({
      kind: "llm_prompt_template",
      user_term: "llm_prompt_template:node-1",
      llm_draft: "Summarise {{ row.body }} for an auditor.",
    });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(
      "Reviewing an LLM-drafted prompt — pick " +
        `${ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL}, then ` +
        `${ACKNOWLEDGEMENT_APPROVE_LABEL} on the review card to continue.`,
    );
    expect(textarea.placeholder).not.toContain(ACKNOWLEDGEMENT_ACCEPT_LABEL);
    expect(textarea.placeholder).not.toContain(ACKNOWLEDGEMENT_AMEND_LABEL);
    expect(textarea.placeholder).not.toContain("llm_prompt_template:node-1");
  });

  it("drops the Change… half for kinds without the amend affordance (kind-aware, ux-review 2026-08-13)", () => {
    // supportsAmendment gates "Change…" to vague_term / legacy-null cards;
    // a model-choice card renders Acknowledge alone, so the cue must too.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({
      kind: "llm_model_choice",
      user_term: "a good model",
      llm_draft: "anthropic/claude-sonnet-4.6",
    });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    // The NOUN is kind-branched too (ux-review 2026-08-13): an
    // llm_model_choice is the composer's pick, not "your interpretation".
    expect(textarea.placeholder).toBe(
      "Reviewing the model the composer picked — pick " +
        `${ACKNOWLEDGEMENT_ACCEPT_LABEL} on the review card to continue.`,
    );
    expect(textarea.placeholder).not.toContain(ACKNOWLEDGEMENT_AMEND_LABEL);
  });

  // ── Per-kind subject noun (ux-review 2026-08-13) ─────────────────────────
  //
  // The fallback branch used to call EVERY kind "your interpretation of X".
  // That is a category error, not a loose synonym: a pipeline_decision, an
  // llm_model_choice and an invented_source are things the COMPOSER chose,
  // and telling an operator they are their own interpretation reassigns
  // authorship at the moment they are asked to attest.  Only vague_term /
  // legacy-null echo the term, because only there is the term the user's.
  it.each([
    [
      "pipeline_decision" as const,
      "Reviewing a decision the composer made",
    ],
    [
      "invented_source" as const,
      "Reviewing source data the composer invented",
    ],
  ])("names %s as the composer's choice, not the user's interpretation", (
    kind,
    expectedSubject,
  ) => {
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({ kind, user_term: "the fast path" });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(
      `${expectedSubject} — pick ${ACKNOWLEDGEMENT_ACCEPT_LABEL} ` +
        "on the review card to continue.",
    );
    // The authorship claim is the whole point of the fix.
    expect(textarea.placeholder).not.toContain("your interpretation");
  });

  // ── Set-aware cue (ux-review 2026-08-13) ─────────────────────────────────
  //
  // The selector used to return on the FIRST pending event carrying a
  // user_term and speak in the singular, so a mixed pending set named
  // controls some visible cards do not render — the elspeth-0a9f77dd75
  // defect surviving in this sibling surface.  subscriptions.ts already
  // solved it; both callers now share characterisePendingControls.
  it("names no control and pluralises when the pending set is mixed", () => {
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    // A prompt review plus a term review from the same turn — routine.
    const promptEvent = makePendingEvent({
      id: "evt-prompt",
      kind: "llm_prompt_template",
      user_term: "llm_prompt_template:node-1",
    });
    const termEvent = makePendingEvent({ id: "evt-term", user_term: "cool" });
    useInterpretationEventsStore.setState({
      pendingBySession: {
        [ACTIVE_SESSION_ID]: {
          [promptEvent.id]: promptEvent,
          [termEvent.id]: termEvent,
        },
      },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(
      "Reviewing 2 composer choices — use the buttons on the review cards " +
        "to continue.",
    );
    // No single control name is true of both cards, so name none of them.
    for (const label of [
      ACKNOWLEDGEMENT_ACCEPT_LABEL,
      ACKNOWLEDGEMENT_AMEND_LABEL,
      ACKNOWLEDGEMENT_APPROVE_LABEL,
      ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
    ]) {
      expect(textarea.placeholder).not.toContain(label);
    }
  });

  it("keeps naming the shared controls when every pending card is the same kind", () => {
    // The mixed-set fallback must not swallow the case it does NOT apply to:
    // two prompt cards still render View prompt + Approve on each.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const first = makePendingEvent({
      id: "evt-a",
      kind: "llm_prompt_template",
      user_term: "llm_prompt_template:node-1",
    });
    const second = makePendingEvent({
      id: "evt-b",
      kind: "llm_prompt_template",
      user_term: "llm_prompt_template:node-2",
    });
    useInterpretationEventsStore.setState({
      pendingBySession: {
        [ACTIVE_SESSION_ID]: { [first.id]: first, [second.id]: second },
      },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(
      "Reviewing 2 LLM-drafted prompts — pick " +
        `${ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL}, then ` +
        `${ACKNOWLEDGEMENT_APPROVE_LABEL} on each review card to continue.`,
    );
  });

  it("does not show the cue when the pending event has user_term=null (auto-baked row)", () => {
    // Auto-baked rows (auto_interpreted_opt_out / no_surfaces) have no term
    // to echo.  Cue falls through to the empty-state placeholder.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({
      user_term: null,
      kind: null,
      interpretation_source: "auto_interpreted_opt_out",
      model_identifier: null,
      model_version: null,
      provider: null,
    });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(EMPTY_STATE);
  });

  it("does not show the cue when there is no active session", () => {
    // Pending events keyed under a different session must not leak through
    // when activeSessionId is null.
    const event = makePendingEvent({ user_term: "cool" });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe(EMPTY_STATE);
  });

  it("respects an explicit `placeholder` prop override even when a pending cue would otherwise fire", () => {
    // Pins the precedence contract: explicit prop > pending-interpretation
    // cue > empty-state.  Guided-mode per-step nudges (Phase A slice 4)
    // remain authoritative even when an interpretation review is open.
    useSessionStore.setState({ activeSessionId: ACTIVE_SESSION_ID });
    const event = makePendingEvent({ user_term: "cool" });
    useInterpretationEventsStore.setState({
      pendingBySession: { [ACTIVE_SESSION_ID]: { [event.id]: event } },
    });

    render(<StandaloneHarness placeholder="step-specific nudge" />);

    const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe("step-specific nudge");
  });
});

// ============================================================================
// ChatInput — tutorial readOnly lock.
//
// The guided tutorial reuses the REAL guided flow; the only difference is the
// STEP_1 "Describe what you want" prompt is prepopulated AND locked, so the
// learner steps through the normal flow but types nothing. This pins that lock:
// the textarea shows the prepopulated value, is read-only, hides the
// source-composition affordances, and Send still submits the locked value.
// ============================================================================
describe("ChatInput — tutorial readOnly lock (prepopulated + locked prompt)", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
    // Post-boot: the backend wall clock has landed, so the readiness gate is
    // open and the learner can press Send on the locked prompt.
    useSessionStore.setState({ composeTimeoutReady: true });
  });

  const LOCKED = "Scrape these three synthetic project-brief pages.";

  function LockedHarness({ onSend }: { onSend: (c: string) => void }) {
    const inputRef = useRef<HTMLTextAreaElement>(
      null,
    ) as RefObject<HTMLTextAreaElement>;
    return (
      <ChatInput
        onSend={onSend}
        disabled={false}
        inputRef={inputRef}
        value={LOCKED}
        onChange={() => undefined}
        readOnly
        onOpenSecrets={() => undefined}
        onToggleBlobManager={() => undefined}
      />
    );
  }

  it("prepopulates the textarea, locks it read-only, and hides source-composition affordances", () => {
    render(<LockedHarness onSend={() => undefined} />);
    const textarea = screen.getByLabelText(
      /message input/i,
    ) as HTMLTextAreaElement;
    expect(textarea.value).toBe(LOCKED);
    expect(textarea.readOnly).toBe(true);
    // The tutorial learner must not author a source by hand: the upload, file
    // manager, and secrets affordances are hidden in locked mode.
    expect(screen.queryByLabelText(/upload file/i)).toBeNull();
    expect(
      screen.queryByLabelText(/show file manager|hide file manager/i),
    ).toBeNull();
    expect(screen.queryByLabelText(/open secrets settings/i)).toBeNull();
  });

  it("Send submits the locked value (the learner presses Send, types nothing)", async () => {
    const sent: string[] = [];
    render(<LockedHarness onSend={(c) => sent.push(c)} />);
    await userEvent.click(screen.getByLabelText(/send message/i));
    expect(sent).toEqual([LOCKED]);
  });
});

describe("ChatInput — compose timeout readiness gate (bootstrap race)", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  function renderInput(onSend: (c: string) => void) {
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;
    render(<ChatInput onSend={onSend} disabled={false} inputRef={inputRef} />);
  }

  it("gates Send behind a visible connecting reason until the timeout is ready", async () => {
    // composeTimeoutReady defaults false (boot). No send may schedule a
    // compose-abort timer from the stale default ceiling; the disabled Send
    // is not a dead button — a visible status says why (dead-button doctrine).
    const onSend = vi.fn();
    renderInput(onSend);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/message input/i), "build a pipeline");

    expect(screen.getByLabelText(/send message/i)).toBeDisabled();
    expect(
      screen.getByText(/connecting to the composer/i),
    ).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("enables Send once the backend wall clock has landed", async () => {
    useSessionStore.setState({ composeTimeoutReady: true });
    const onSend = vi.fn();
    renderInput(onSend);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/message input/i), "build a pipeline");

    expect(screen.getByLabelText(/send message/i)).toBeEnabled();
    expect(screen.queryByText(/connecting to the composer/i)).toBeNull();

    await user.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("build a pipeline");
  });

  it("shows a distinct unavailable alert (not 'Connecting…') when the backend reports no compose timeout", () => {
    // Backend up but no usable timeout: readiness never latches, so surface a
    // stuck-state diagnostic instead of a perpetual soft "Connecting…".
    useSessionStore.setState({ composerTimeoutUnavailable: true });
    renderInput(vi.fn());

    expect(
      screen.getByText(/server did not report a compose timeout/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/connecting to the composer/i)).toBeNull();
    expect(screen.getByLabelText(/send message/i)).toBeDisabled();
  });
});

describe("ChatInput — rare-action overflow (elspeth-8fa71e6d15)", () => {
  // The file-manager toggle and secrets entry fold behind one "More"
  // trigger so the textarea keeps a usable width in the 360px authoring
  // column. Upload stays persistent — it is the core "give the composer
  // your data" action.
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  function renderInput(overrides?: {
    onToggleBlobManager?: () => void;
    onOpenSecrets?: () => void;
    readOnly?: boolean;
  }) {
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;
    return render(
      <ChatInput
        onSend={() => undefined}
        disabled={false}
        inputRef={inputRef}
        onToggleBlobManager={overrides?.onToggleBlobManager}
        onOpenSecrets={overrides?.onOpenSecrets}
        readOnly={overrides?.readOnly}
      />,
    );
  }

  it("keeps only Upload persistent — folder and key live behind More", () => {
    renderInput({
      onToggleBlobManager: vi.fn(),
      onOpenSecrets: vi.fn(),
    });
    expect(screen.getByLabelText(/upload file/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/more actions/i)).toBeInTheDocument();
    // Closed menu: neither rare action renders as a top-level row button.
    expect(
      screen.queryByLabelText(/show file manager|hide file manager/i),
    ).toBeNull();
    expect(screen.queryByLabelText(/open secrets settings/i)).toBeNull();
  });

  it("opens the menu, dispatches each action, and closes on selection", async () => {
    const onToggleBlobManager = vi.fn();
    const onOpenSecrets = vi.fn();
    renderInput({ onToggleBlobManager, onOpenSecrets });

    const trigger = screen.getByLabelText(/more actions/i);
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(screen.getByLabelText(/show file manager/i));
    expect(onToggleBlobManager).toHaveBeenCalledTimes(1);
    // Selection closes the menu.
    expect(screen.queryByLabelText(/show file manager/i)).toBeNull();

    await userEvent.click(trigger);
    await userEvent.click(screen.getByLabelText(/open secrets settings/i));
    expect(onOpenSecrets).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText(/open secrets settings/i)).toBeNull();
  });

  it("closes on Escape without dispatching", async () => {
    const onToggleBlobManager = vi.fn();
    renderInput({ onToggleBlobManager, onOpenSecrets: vi.fn() });

    await userEvent.click(screen.getByLabelText(/more actions/i));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByLabelText(/show file manager/i)).toBeNull();
    expect(onToggleBlobManager).not.toHaveBeenCalled();
  });

  it("renders no More trigger when both rare actions are absent or locked", () => {
    renderInput();
    expect(screen.queryByLabelText(/more actions/i)).toBeNull();

    renderInput({
      onToggleBlobManager: vi.fn(),
      onOpenSecrets: vi.fn(),
      readOnly: true,
    });
    expect(screen.queryByLabelText(/more actions/i)).toBeNull();
  });

  it("pins the narrow-pane wrap mechanics in chat.css (container query, break, floor)", () => {
    // The wrap rule keys off the PANE's width (container query), not the
    // viewport — 360px is the shipped default pane width at 1280-1535px
    // viewports, which no viewport media query can see. The min-width floor
    // is the backstop that makes a wrap-rule regression visible.
    const css = readFileSync("src/components/chat/chat.css", "utf8");
    expect(css).toMatch(/\.chat-input\s*\{[^}]*container-type:\s*inline-size/s);
    const textareaRule = css.match(/\.chat-input-textarea\s*\{([^}]*)\}/s)?.[1];
    expect(textareaRule).toMatch(/min-width:\s*160px/);
    const containerBlock = css.match(
      /@container \(max-width: 429px\)\s*\{([\s\S]*?)\n\}/,
    )?.[1];
    expect(containerBlock).toBeDefined();
    expect(containerBlock).toMatch(
      /\.chat-input-row-break\s*\{[^}]*flex-basis:\s*100%/s,
    );
    expect(containerBlock).toMatch(
      /\.chat-input-upload-btn,\s*\.chat-input-more\s*\{[^}]*order:\s*2/s,
    );
  });
});

// ============================================================================
// ChatInput — placeholder legibility and the overflow glyph.
// ============================================================================

describe("ChatInput placeholder legibility (elspeth-244b8ba932)", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  function renderInput(props?: { readOnly?: boolean; value?: string }) {
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;
    return render(
      <ChatInput
        onSend={() => undefined}
        disabled={false}
        inputRef={inputRef}
        readOnly={props?.readOnly}
        value={props?.value}
        onChange={() => undefined}
      />,
    );
  }

  // A placeholder produces NO scroll overflow — no scrollbar, no ellipsis — so
  // a placeholder taller than the box is simply cut mid-word with no cue that
  // anything is missing. Every shipped placeholder here is a full sentence
  // (the four guided per-step nudges, the empty-state priming line, the
  // pending-interpretation cue), and two rows clipped them in the 360px
  // authoring pane. Autosizing cannot rescue this: there is no scrollHeight to
  // measure when the box is empty. The row count IS the fix, so pin it.
  it("gives the editable composer three rows, not two", () => {
    renderInput();
    expect(screen.getByLabelText(/message input/i)).toHaveAttribute("rows", "3");
  });

  it("still sizes the read-only tutorial prompt to its content", () => {
    // The locked worked-example prompt is static and multi-line; it is sized
    // to the content (capped at 10) rather than to the editable row count.
    renderInput({ readOnly: true, value: "a\nb\nc\nd\ne" });
    expect(screen.getByLabelText(/message input/i)).toHaveAttribute("rows", "6");
  });

  it("caps read-only growth at ten rows", () => {
    renderInput({ readOnly: true, value: Array(40).fill("line").join("\n") });
    expect(screen.getByLabelText(/message input/i)).toHaveAttribute(
      "rows",
      "10",
    );
  });

  // The growth ceiling pinned above ("bounds textarea growth against the
  // desktop dynamic viewport") is NOT dead code and must not be "cleaned up"
  // on the grounds that nothing autosizes into it: `resize: vertical` is what
  // reaches it, and the ceiling is what bounds the box the user drags.
  it("keeps the user-drag affordance the growth ceiling exists to bound", () => {
    const css = readFileSync("src/components/chat/chat.css", "utf8");
    const rule = css.match(/\.chat-input-textarea\s*\{([^}]*)\}/s)?.[1];
    expect(rule).toMatch(/resize:\s*vertical/);
  });
});

describe("ChatInput overflow glyph (elspeth-b720e0b932)", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    resetStore(useInterpretationEventsStore);
  });

  function renderInput() {
    const inputRef = { current: null } as RefObject<HTMLTextAreaElement>;
    return render(
      <ChatInput
        onSend={() => undefined}
        disabled={false}
        inputRef={inputRef}
        onToggleBlobManager={() => undefined}
        onOpenSecrets={() => undefined}
      />,
    );
  }

  // The "…" trigger used to be three ZERO-LENGTH path segments
  // ("M5 12h.01M12 12h.01M19 12h.01") relying on `stroke-linecap: round` to
  // become dots. At 20px on a 24-unit viewBox that cap is 1.5px, which cannot
  // rasterise round and carries roughly an order of magnitude less ink than
  // the Upload glyph in the identically-sized button beside it — so the
  // control read as disabled. Explicit filled circles are the fix.
  it("draws the overflow trigger as filled circles, not zero-length strokes", () => {
    renderInput();
    const svg = screen
      .getByLabelText(/more actions/i)
      .querySelector("svg.chat-input-icon");
    expect(svg).not.toBeNull();

    const circles = Array.from(svg!.querySelectorAll("circle"));
    expect(circles).toHaveLength(3);
    for (const circle of circles) {
      // A presentation attribute ON the circle beats the `fill: none` it would
      // otherwise INHERIT from .chat-input-icon, and currentColor keeps the
      // glyph tracking --color-text in both themes.
      expect(circle.getAttribute("fill")).toBe("currentColor");
      expect(Number(circle.getAttribute("r"))).toBeGreaterThan(0);
    }
    // Distinct centres — three dots, not one drawn three times.
    expect(new Set(circles.map((c) => c.getAttribute("cx"))).size).toBe(3);

    // No stroked path survives on this glyph, so the zero-length-segment
    // idiom cannot creep back in beside the circles.
    expect(svg!.querySelector("path")).toBeNull();
  });

  it("leaves the sibling Upload glyph a stroked path", () => {
    renderInput();
    const svg = screen
      .getByLabelText(/upload file/i)
      .querySelector("svg.chat-input-icon");
    expect(svg!.querySelectorAll("path")).toHaveLength(1);
    expect(svg!.querySelectorAll("circle")).toHaveLength(0);
  });

  it("keeps the round-cap stroke settings the other glyphs in the family rely on", () => {
    const css = readFileSync("src/components/chat/chat.css", "utf8");
    const rule = css.match(/\.chat-input-icon\s*\{([^}]*)\}/s)?.[1];
    expect(rule).toMatch(/stroke:\s*currentColor/);
    expect(rule).toMatch(/fill:\s*none/);
  });
});
