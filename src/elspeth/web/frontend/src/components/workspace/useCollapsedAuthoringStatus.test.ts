import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { InterpretationEvent } from "@/types/interpretation";
import { useCollapsedAuthoringStatus } from "./useCollapsedAuthoringStatus";

const SESSION_A = "session-a";

function message(id: string) {
  return {
    id,
    session_id: SESSION_A,
    role: "assistant",
    content: "Update",
    created_at: "2026-08-11T00:00:00Z",
  } as never;
}

function pendingEvent(id: string): InterpretationEvent {
  return {
    id,
    session_id: SESSION_A,
    composition_state_id: "state-1",
    affected_node_id: null,
    tool_call_id: "tool-1",
    user_term: "term",
    kind: "vague_term",
    llm_draft: "draft",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-08-11T00:00:00Z",
    resolved_at: null,
    actor: "system:composer",
    interpretation_source: "user_approved",
    model_identifier: "model",
    model_version: null,
    provider: "provider",
    composer_skill_hash: "0".repeat(64),
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    resolved_prompt_template_hash: null,
  };
}

describe("useCollapsedAuthoringStatus", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useInterpretationEventsStore);
    // compositionStateLoaded: the store flips this true in the SAME set()
    // that lands the fetched message history (selectSession), so it is the
    // hook's "history has hydrated" signal. The live-update tests below
    // model a loaded session receiving new activity.
    useSessionStore.setState({
      activeSessionId: SESSION_A,
      compositionStateLoaded: true,
    });
  });

  it("does not count history hydration as unread on a collapsed refresh", () => {
    // Refresh-while-collapsed (2026-08-15): the page mounts collapsed with
    // an EMPTY store (selectSession clears messages and sets
    // compositionStateLoaded false before its fetch), then the WHOLE
    // transcript lands in one set(). Those messages are history, not news —
    // a 12-message transcript read as "12 new messages" with zero actual
    // authoring activity.
    useSessionStore.setState({
      compositionStateLoaded: false,
      messages: [],
    });
    const { result } = renderHook(() =>
      useCollapsedAuthoringStatus({
        activeSessionId: SESSION_A,
        authoringCollapsed: true,
      }),
    );
    expect(result.current).toEqual({
      text: "Authoring pane collapsed",
      tone: "neutral",
    });

    act(() =>
      useSessionStore.setState({
        messages: [message("m1"), message("m2"), message("m3")],
        compositionStateLoaded: true,
      }),
    );
    expect(result.current).toEqual({
      text: "Authoring pane collapsed",
      tone: "neutral",
    });

    // A message arriving AFTER hydration is genuinely new.
    act(() =>
      useSessionStore.setState({
        messages: [
          message("m1"),
          message("m2"),
          message("m3"),
          message("m4"),
        ],
      }),
    );
    expect(result.current).toEqual({ text: "1 new message", tone: "busy" });
  });

  it("uses error, busy, unread, then neutral priority", () => {
    const { result, rerender } = renderHook(
      ({ collapsed }) =>
        useCollapsedAuthoringStatus({
          activeSessionId: SESSION_A,
          authoringCollapsed: collapsed,
        }),
      { initialProps: { collapsed: false } },
    );

    rerender({ collapsed: true });
    expect(result.current).toEqual({
      text: "Authoring pane collapsed",
      tone: "neutral",
    });

    act(() => useSessionStore.setState({ messages: [message("m1")] }));
    expect(result.current).toEqual({ text: "1 new message", tone: "busy" });

    act(() => useSessionStore.setState({ isComposing: true }));
    expect(result.current).toEqual({
      text: "Authoring in progress",
      tone: "busy",
    });

    act(() => useSessionStore.setState({ error: "Planner unavailable" }));
    expect(result.current).toEqual({
      text: "Authoring error: Planner unavailable",
      tone: "error",
    });
  });

  it.each(["guidedChatPending", "guidedResponsePending"] as const)(
    "shows busy while %s is true",
    (field) => {
      const { result, rerender } = renderHook(
        ({ collapsed }) =>
          useCollapsedAuthoringStatus({
            activeSessionId: SESSION_A,
            authoringCollapsed: collapsed,
          }),
        { initialProps: { collapsed: false } },
      );
      rerender({ collapsed: true });

      act(() => useSessionStore.setState({ [field]: true }));

      expect(result.current).toEqual({
        text: "Authoring in progress",
        tone: "busy",
      });
    },
  );

  it("reports new messages and acknowledgements received while collapsed", () => {
    const { result, rerender } = renderHook(
      ({ collapsed }) =>
        useCollapsedAuthoringStatus({
          activeSessionId: SESSION_A,
          authoringCollapsed: collapsed,
        }),
      { initialProps: { collapsed: false } },
    );
    rerender({ collapsed: true });

    act(() => {
      useSessionStore.setState({ messages: [message("m1"), message("m2")] });
      useInterpretationEventsStore.setState({
        pendingBySession: {
          [SESSION_A]: { e1: pendingEvent("e1") },
        },
      });
    });

    expect(result.current).toEqual({
      text: "2 new messages, 1 decision to acknowledge",
      tone: "busy",
    });
  });

  it("resets unread baseline when restored and when the session changes", () => {
    const { result, rerender } = renderHook(
      ({ collapsed, sessionId }) =>
        useCollapsedAuthoringStatus({
          activeSessionId: sessionId,
          authoringCollapsed: collapsed,
        }),
      { initialProps: { collapsed: false, sessionId: SESSION_A } },
    );
    rerender({ collapsed: true, sessionId: SESSION_A });
    act(() => useSessionStore.setState({ messages: [message("m1")] }));
    expect(result.current.text).toBe("1 new message");

    rerender({ collapsed: false, sessionId: SESSION_A });
    rerender({ collapsed: true, sessionId: SESSION_A });
    expect(result.current.text).toBe("Authoring pane collapsed");

    act(() => useSessionStore.setState({ messages: [message("other")] }));
    rerender({ collapsed: true, sessionId: "session-b" });
    expect(result.current.text).toBe("Authoring pane collapsed");
  });
});
