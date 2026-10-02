import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AcknowledgementStack } from "@/components/chat/AcknowledgementStack";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { InterpretationEvent } from "@/types/interpretation";

// Keep the API client, error-envelope parser, store and rendered card real.
// Only the HTTP transport is replaced, using the backend's actual envelopes.
const event: InterpretationEvent = {
  id: "event-a",
  session_id: "session-1",
  composition_state_id: "state-1",
  affected_node_id: "variant_a",
  tool_call_id: "tool-a",
  user_term: "prompt_template",
  kind: "llm_prompt_template",
  llm_draft: "System: Summarise precisely.\nUser: Summarise {{ row.text }}.",
  accepted_value: null,
  choice: "pending",
  created_at: "2026-09-22T00:00:00Z",
  resolved_at: null,
  actor: "user:owner",
  interpretation_source: "user_approved",
  model_identifier: "test-model",
  model_version: null,
  provider: "test-provider",
  composer_skill_hash: "test-hash",
  arguments_hash: null,
  hash_domain_version: null,
  runtime_model_identifier_at_resolve: null,
  runtime_model_version_at_resolve: null,
  approved_prompt_artifact_hash: null,
};

function response(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  resetStore(useInterpretationEventsStore);
  resetStore(useSessionStore);
  useSessionStore.setState({ compositionState: {
    id: "state-1", ...compositionStateAuthorityFields, version: 1,
    sources: {}, edges: [], outputs: [], metadata: { name: null, description: null },
    nodes: [{ id: "variant_a", node_type: "transform", plugin: "llm", input: "rows", on_success: null, on_error: null,
      options: { system_prompt: "Summarise precisely.", prompt_template: "Summarise {{ row.text }}." } }],
  } });
  useInterpretationEventsStore.getState().addPendingEvent("session-1", event);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("interpretation approval HTTP conflicts", () => {
  it.each([
    { detail: "Session operation is already active" },
    { detail: { code: "another_conflict", message: "The session changed. Review the current draft." } },
  ])("preserves an unapproved card and permits retry after $detail", async (body) => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(body, 409))
      .mockResolvedValueOnce(response({
        event: {
          ...event,
          choice: "accepted_as_drafted",
          accepted_value: event.llm_draft,
          resolved_at: "2026-09-22T00:01:00Z",
        },
        new_state: {
          id: "state-2",
          ...compositionStateAuthorityFields,
          version: 2,
          sources: {},
          nodes: [],
          edges: [],
          outputs: [],
          metadata: { name: null, description: null },
        },
      }, 200));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<AcknowledgementStack sessionId="session-1" />);
    await user.click(screen.getByRole("button", { name: /view prompt/i }));
    await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));

    const alert = await screen.findByRole("alert");
    const detail = typeof body.detail === "string" ? body.detail : body.detail.message;
    expect(alert).toHaveTextContent(detail);
    expect(alert).not.toHaveTextContent(/already resolved|another tab|already approved/i);
    expect(useInterpretationEventsStore.getState().pendingBySession["session-1"][event.id]).toEqual(event);
    expect(useInterpretationEventsStore.getState().resolvedBySession["session-1"]).toBeUndefined();
    expect(screen.getByRole("button", { name: /approve the llm prompt template/i })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));
    await waitFor(() => expect(screen.queryByTestId("acknowledgement-stack")).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, options] of fetchMock.mock.calls) {
      expect(url).toBe("/api/sessions/session-1/interpretations/event-a/resolve");
      expect(options?.body).toBe(JSON.stringify({ choice: "accepted_as_drafted" }));
    }
    expect(useInterpretationEventsStore.getState().pendingBySession["session-1"]).toEqual({});
    expect(useInterpretationEventsStore.getState().resolvedCountBySession["session-1"].accepted_as_drafted).toBe(1);
  });

  it("recognises a coded resolved response without inventing who resolved it", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(response({
      detail: { code: "interpretation_already_resolved", message: "Interpretation event is already resolved." },
    }, 409)));
    const user = userEvent.setup();
    render(<AcknowledgementStack sessionId="session-1" />);
    await user.click(screen.getByRole("button", { name: /view prompt/i }));
    await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Already resolved");
    expect(alert).toHaveTextContent(/reload/i);
    expect(alert).not.toHaveTextContent(/another tab|approved/i);
    expect(useInterpretationEventsStore.getState().resolvedBySession["session-1"]).toBeUndefined();
  });

  it("preserves an opt-out conflict without claiming a decision was resolved", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(response({
      detail: "Session operation is already active",
    }, 409)));
    const user = userEvent.setup();
    render(<AcknowledgementStack sessionId="session-1" />);
    await user.click(screen.getByRole("button", { name: "Stop reviewing interpretations this session" }));
    await user.click(screen.getByRole("button", { name: "Stop reviewing for this session" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Session operation is already active");
    expect(alert).not.toHaveTextContent(/already resolved|another tab/i);
    expect(useInterpretationEventsStore.getState().optedOutBySession["session-1"]).toBeUndefined();
    expect(useInterpretationEventsStore.getState().pendingBySession["session-1"][event.id]).toEqual(event);
  });
});
