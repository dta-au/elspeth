// ============================================================================
// AcknowledgementCard — behavioural coverage ported from the retired
// InterpretationReviewTurn / InterpretationReviewInlineMessage suites.
//
// Discipline: mock ONLY the API client's resolve / opt-out methods; the
// Zustand store runs live so the wire path stays honest.  "Acknowledge" ==
// today's accept (`accepted_as_drafted`).
// ============================================================================

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ComponentProps } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AcknowledgementCard } from "./AcknowledgementCard";
import {
  ACKNOWLEDGEMENT_AMEND_LABEL,
  ACKNOWLEDGEMENT_APPROVE_LABEL,
  ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
} from "./acknowledgementLabels";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import type {
  InterpretationEvent,
  InterpretationResolveResponse,
} from "@/types/interpretation";
import type { CompositionState } from "@/types/api";
import type { ApiError } from "@/types/index";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";

vi.mock("@/api/client", () => ({
  listInterpretationEvents: vi.fn(),
  resolveInterpretation: vi.fn(),
  optOutOfInterpretations: vi.fn(),
  getInterpretationOptOutSummary: vi.fn(),
}));

import * as api from "@/api/client";

function makeEvent(
  overrides: Partial<InterpretationEvent> = {},
): InterpretationEvent {
  return {
    id: "evt-1",
    session_id: "sess-1",
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
    ...overrides,
  };
}

function makeCompositionState(version = 2): CompositionState {
  return {
    id: `state-${version}`,
    ...compositionStateAuthorityFields,
    version,
    sources: {},
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}

function makeResolveResponse(
  event: InterpretationEvent,
  overrides: Partial<InterpretationResolveResponse> = {},
): InterpretationResolveResponse {
  return {
    event: {
      ...event,
      choice: "accepted_as_drafted",
      accepted_value: event.llm_draft,
      resolved_at: "2026-05-18T01:00:00Z",
    },
    new_state: makeCompositionState(2),
    ...overrides,
  };
}

function makeApiError(status: number, detail = ""): ApiError {
  return {
    status,
    detail,
    error_type: undefined,
    partial_state: undefined,
    failed_turn: undefined,
    partial_state_save_failed: undefined,
    partial_state_save_error: undefined,
    fanout_guard: undefined,
    provider_detail: undefined,
    provider_status_code: undefined,
    validation_errors: undefined,
  };
}

function renderCard(
  event: InterpretationEvent,
  props: Partial<ComponentProps<typeof AcknowledgementCard>> = {},
) {
  return render(
    <AcknowledgementCard
      event={event}
      sessionId="sess-1"
      stepLabel={props.stepLabel ?? "Summarise"}
      compositionState={props.compositionState}
      showAmend={props.showAmend ?? event.kind === "vague_term"}
      onResolved={props.onResolved}
    />,
  );
}

beforeEach(() => {
  resetStore(useInterpretationEventsStore);
  vi.mocked(api.resolveInterpretation).mockReset();
  vi.mocked(api.optOutOfInterpretations).mockReset();
  vi.mocked(api.listInterpretationEvents).mockReset();
});

// ── Copy & title ─────────────────────────────────────────────────────────────

describe("AcknowledgementCard — per-kind copy", () => {
  it("vague_term: title 'Interpretation' + user_term/llm_draft line", () => {
    renderCard(makeEvent({ user_term: "cool", llm_draft: "trendy" }));
    expect(screen.getByText("Interpretation")).toBeTruthy();
    expect(screen.getByText(/cool/)).toBeTruthy();
    expect(screen.getByText(/trendy/)).toBeTruthy();
  });

  it("llm_model_choice: '<step> step · model' title + 'The LLM picked' line", () => {
    renderCard(
      makeEvent({ kind: "llm_model_choice", llm_draft: "openai/gpt-4o-mini" }),
      { stepLabel: "Summarise", showAmend: false },
    );
    expect(screen.getByText("Summarise step · model")).toBeTruthy();
    expect(screen.getByText(/The LLM picked/)).toBeTruthy();
    expect(screen.getByText("openai/gpt-4o-mini")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /acknowledge the llm model choice/i }),
    ).toBeTruthy();
  });

  it("pipeline_decision: '<step> step · decision' title + decision text inline", () => {
    renderCard(
      makeEvent({
        kind: "pipeline_decision",
        llm_draft: "Drop the scraped raw HTML before saving the JSON output.",
      }),
      { stepLabel: "Output", showAmend: false },
    );
    expect(screen.getByText("Output step · decision")).toBeTruthy();
    expect(screen.getByText(/drop the scraped raw html/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /acknowledge the pipeline decision/i }),
    ).toBeTruthy();
  });

  it("invented_source: title 'Source data' + review-before-fetching line", () => {
    renderCard(
      makeEvent({
        kind: "invented_source",
        llm_draft: '{"name":"Ada","amount":42}',
      }),
      { showAmend: false },
    );
    expect(screen.getByText("Source data")).toBeTruthy();
    expect(screen.getByText(/invented this source data/i)).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: /acknowledge the invented source data/i,
      }),
    ).toBeTruthy();
  });

  it("hides the amend affordance when showAmend is false", () => {
    renderCard(makeEvent(), { showAmend: false });
    expect(
      screen.queryByRole("button", { name: /change the interpretation/i }),
    ).toBeNull();
  });
});

// ── Value rendering ──────────────────────────────────────────────────────────

describe("AcknowledgementCard — invented-source value", () => {
  it("pretty-prints a short JSON source value inline", () => {
    const { container } = renderCard(
      makeEvent({
        kind: "invented_source",
        llm_draft: '{"name":"Ada","amount":42}',
      }),
      { showAmend: false },
    );
    const pre = container.querySelector("pre");
    expect(pre!.getAttribute("data-codeblock-format")).toBe("json");
    // No View expander for a short value.
    expect(screen.queryByRole("button", { name: /^view$/i })).toBeNull();
  });

  it("falls back to plain monospace for a non-JSON source value", () => {
    const { container } = renderCard(
      makeEvent({ kind: "invented_source", llm_draft: "name,amount\nAda,42" }),
      { showAmend: false },
    );
    const pre = container.querySelector("pre");
    expect(pre!.getAttribute("data-codeblock-format")).toBe("plain");
  });

  it("puts a long source value behind a View expander", async () => {
    const user = userEvent.setup();
    const longJson = JSON.stringify(
      Array.from({ length: 40 }, (_, i) => ({ row: i, label: `item-${i}` })),
    );
    renderCard(
      makeEvent({ kind: "invented_source", llm_draft: longJson }),
      { showAmend: false },
    );
    const view = screen.getByRole("button", { name: /^view$/i });
    expect(view).toBeTruthy();
    await user.click(view);
    expect(screen.getByRole("button", { name: /^hide$/i })).toBeTruthy();
  });
});

// ── Acknowledge → accepted_as_drafted ────────────────────────────────────────

describe("AcknowledgementCard — Acknowledge", () => {
  it("resolves with choice='accepted_as_drafted'", async () => {
    const user = userEvent.setup();
    const event = makeEvent();
    vi.mocked(api.resolveInterpretation).mockResolvedValue(
      makeResolveResponse(event),
    );

    renderCard(event);
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );

    await waitFor(() => {
      expect(api.resolveInterpretation).toHaveBeenCalledWith("sess-1", "evt-1", {
        choice: "accepted_as_drafted",
      });
    });
  });

  it("fires onResolved with the new composition state", async () => {
    const user = userEvent.setup();
    const event = makeEvent();
    const newState = makeCompositionState(3);
    vi.mocked(api.resolveInterpretation).mockResolvedValue(
      makeResolveResponse(event, { new_state: newState }),
    );
    const onResolved = vi.fn();

    renderCard(event, { onResolved });
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );

    await waitFor(() => {
      expect(onResolved).toHaveBeenCalledWith(newState);
    });
  });

  it("disables both primary buttons and shows a spinner while in flight", async () => {
    const user = userEvent.setup();
    const event = makeEvent();
    let resolveResolve: (v: InterpretationResolveResponse) => void = () => {};
    vi.mocked(api.resolveInterpretation).mockImplementation(
      () =>
        new Promise<InterpretationResolveResponse>((res) => {
          resolveResolve = res;
        }),
    );

    renderCard(event);
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );

    const accept = screen.getByRole("button", {
      name: /acknowledge the llm's interpretation of cool/i,
    }) as HTMLButtonElement;
    const change = screen.getByRole("button", {
      name: /change the interpretation of cool/i,
    }) as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    expect(change.disabled).toBe(true);
    expect(screen.getByText(/saving/i)).toBeTruthy();

    resolveResolve(makeResolveResponse(event));
    await waitFor(() => {
      expect(api.resolveInterpretation).toHaveBeenCalledTimes(1);
    });
  });
});

// ── Amend flow ───────────────────────────────────────────────────────────────

describe("AcknowledgementCard — amend", () => {
  it("Change… reveals a textarea pre-filled with llm_draft and focuses it", async () => {
    const user = userEvent.setup();
    renderCard(makeEvent({ llm_draft: "interesting and engaging" }));

    await user.click(
      screen.getByRole("button", { name: /change the interpretation of cool/i }),
    );

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("interesting and engaging");
    expect(document.activeElement).toBe(textarea);
  });

  it("Submit resolves with choice='amended' and the new text", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ llm_draft: "old draft" });
    vi.mocked(api.resolveInterpretation).mockResolvedValue(
      makeResolveResponse(event, {
        event: { ...event, choice: "amended", accepted_value: "my new wording" },
      }),
    );

    renderCard(event);
    await user.click(
      screen.getByRole("button", { name: /change the interpretation of cool/i }),
    );
    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "my new wording");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => {
      expect(api.resolveInterpretation).toHaveBeenCalledWith("sess-1", "evt-1", {
        choice: "amended",
        amended_value: "my new wording",
      });
    });
  });

  it("Cancel reverts to the choose view without a request", async () => {
    const user = userEvent.setup();
    renderCard(makeEvent());

    await user.click(
      screen.getByRole("button", { name: /change the interpretation of cool/i }),
    );
    expect(screen.queryByRole("button", { name: "Submit" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });

  it("Submit is disabled for an empty amendment", async () => {
    const user = userEvent.setup();
    renderCard(makeEvent({ llm_draft: "draft" }));
    await user.click(
      screen.getByRole("button", { name: /change the interpretation of cool/i }),
    );
    await user.clear(screen.getByRole("textbox"));
    expect(
      (screen.getByRole("button", { name: "Submit" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("surfaces the 8 KB cap and blocks the request for an oversized amendment", async () => {
    const user = userEvent.setup();
    renderCard(makeEvent({ llm_draft: "draft" }));
    await user.click(
      screen.getByRole("button", { name: /change the interpretation of cool/i }),
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "a".repeat(8200) } });

    expect(
      (screen.getByRole("button", { name: "Submit" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.getByText(/8192 bytes/)).toBeTruthy();
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });
});

// ── Error mapping ────────────────────────────────────────────────────────────

describe("AcknowledgementCard — error mapping", () => {
  it("409 → already-resolved-in-another-tab message", async () => {
    const user = userEvent.setup();
    vi.mocked(api.resolveInterpretation).mockRejectedValue(makeApiError(409));
    renderCard(makeEvent());
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already resolved in another tab/i);
  });

  it("other (500) → generic could-not-resolve message with detail", async () => {
    const user = userEvent.setup();
    vi.mocked(api.resolveInterpretation).mockRejectedValue(
      makeApiError(500, "upstream exploded"),
    );
    renderCard(makeEvent());
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/could not resolve interpretation/i);
    expect(alert.textContent).toMatch(/upstream exploded/i);
  });

  it("422 → validation detail", async () => {
    const user = userEvent.setup();
    vi.mocked(api.resolveInterpretation).mockRejectedValue(
      makeApiError(422, "amended_value must not be blank"),
    );
    renderCard(makeEvent());
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/amended_value must not be blank/i);
  });

  it("422 placeholder-unavailable on a prompt template → stale-review message", async () => {
    const user = userEvent.setup();
    vi.mocked(api.resolveInterpretation).mockRejectedValue({
      ...makeApiError(422, "placeholder gone"),
      error_type: "interpretation_placeholder_unavailable",
    });
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: "Classify {{ x }}." }),
      { showAmend: false },
    );
    // Two-control design: the View prompt toggle unlocks the separate
    // Approve button.
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await user.click(
      screen.getByRole("button", { name: /approve the llm prompt template/i }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/stale review/i);
    expect(alert.textContent).toMatch(/reload the session/i);
  });
});

// ── Prompt-template View/Approve controls (elspeth-3a4a65530f) ───────────────
//
// Deliberate rewrite of the retired "two-stage primary" oracles: the single
// morphing button (View prompt → Approve, operator ask 2026-07-03) is
// replaced by two single-meaning controls — a disclosure toggle and an
// Approve button that stays disabled until the prompt has been viewed.

describe("AcknowledgementCard — prompt-template View/Approve controls", () => {
  it("renders two distinct controls pre-view: an enabled View prompt toggle and a disabled Approve", () => {
    renderCard(
      makeEvent({
        kind: "llm_prompt_template",
        llm_draft: "Summarise {{ row.body }} for an auditor.",
      }),
      { showAmend: false },
    );

    const toggle = screen.getByRole("button", {
      name: "View prompt",
    }) as HTMLButtonElement;
    expect(toggle.disabled).toBe(false);
    expect(toggle.className).toContain("ack-card-view-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Truth test for the exported constant (elspeth-0a9f77dd75): the cue
    // prose (ChatInput placeholder, subscriptions system note) interpolates
    // ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL, so the rendered toggle must be
    // byte-exact with it.
    expect(toggle.textContent).toBe(ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL);

    const approve = screen.getByRole("button", {
      name: /approve the llm prompt template/i,
    }) as HTMLButtonElement;
    expect(approve).not.toBe(toggle);
    expect(approve.className).toContain("ack-card-accept-btn");
    // Same truth test for the prompt card's accept label: the card renders
    // "Approve" (never "Acknowledge"), and the exported constant is the
    // anti-drift anchor the pointing prose interpolates.
    expect(approve.textContent).toBe(ACKNOWLEDGEMENT_APPROVE_LABEL);
    expect(ACKNOWLEDGEMENT_APPROVE_LABEL).toBe("Approve");
    // The VIEW GATE is aria-disabled, not native `disabled` (house idiom for
    // a gated primary with an attached reason — ExecuteButton.tsx,
    // elspeth-94c32de486): the button must stay FOCUSABLE so its
    // aria-describedby gate note is reachable by SR navigation.  Inertness is
    // pinned separately by the rapid-double-click test below, which is what
    // keeps this an advisory attribute plus a real no-op rather than a
    // downgrade.
    expect(approve.getAttribute("aria-disabled")).toBe("true");
    expect(approve.disabled).toBe(false);
    expect(approve.tabIndex).toBe(0);

    // DOM/tab order: the prerequisite disclosure precedes the gated primary
    // (ux-review 2026-08-13).  In a 360px pane the toggle otherwise fell
    // below the fold under a greyed-out Approve.
    expect(
      toggle.compareDocumentPosition(approve) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // The prompt is hidden pre-view, and exactly one control carries the
    // name "View prompt" (no duplicate-name trap).
    expect(
      screen.queryByRole("region", { name: /prompt template review/i }),
    ).toBeNull();
    expect(screen.getAllByRole("button", { name: /view prompt/i })).toHaveLength(1);
  });

  it("never resolves from the disabled Approve, even under a rapid double click", () => {
    const event = makeEvent({
      kind: "llm_prompt_template",
      llm_draft: "Summarise {{ row.body }}.",
    });
    vi.mocked(api.resolveInterpretation).mockResolvedValue(
      makeResolveResponse(event),
    );
    renderCard(event, { showAmend: false });

    const approve = screen.getByRole("button", {
      name: /approve the llm prompt template/i,
    });
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });

  it("View prompt reveals the region and unlocks Approve; viewing alone resolves nothing", async () => {
    const user = userEvent.setup();
    const event = makeEvent({
      kind: "llm_prompt_template",
      llm_draft: "Summarise {{ row.body }} for an auditor.",
    });
    vi.mocked(api.resolveInterpretation).mockResolvedValue(
      makeResolveResponse(event),
    );
    renderCard(event, { showAmend: false });

    await user.click(screen.getByRole("button", { name: "View prompt" }));

    expect(
      screen.getByRole("region", { name: /prompt template review/i }),
    ).toBeTruthy();
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
    const approve = screen.getByRole("button", {
      name: /approve the llm prompt template/i,
    }) as HTMLButtonElement;
    expect(approve.disabled).toBe(false);

    await user.click(approve);
    await waitFor(() =>
      expect(api.resolveInterpretation).toHaveBeenCalledTimes(1),
    );
  });

  it("collapsing the prompt after viewing does NOT re-disable Approve", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: "Classify {{ x }}." }),
      { showAmend: false },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await user.click(screen.getByRole("button", { name: "Hide prompt" }));
    expect(
      screen.queryByRole("region", { name: /prompt template review/i }),
    ).toBeNull();
    const approve = screen.getByRole("button", {
      name: /approve the llm prompt template/i,
    }) as HTMLButtonElement;
    expect(approve.disabled).toBe(false);
  });

  // Successor of elspeth-3b35abf148 variant 2: the gate says WHY in visible
  // text wired to the Approve button via aria-describedby.
  it("explains the gate in visible text wired to Approve, cleared once viewed", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({
        kind: "llm_prompt_template",
        llm_draft: "Summarise {{ row.body }}.",
      }),
      { showAmend: false },
    );

    const note = screen.getByText(/approve unlocks once you have viewed it/i);
    expect(note.classList.contains("visually-hidden")).toBe(false);
    const approve = screen.getByRole("button", {
      name: /approve the llm prompt template/i,
    });
    expect(approve.getAttribute("aria-describedby")).toBe(note.id);

    await user.click(screen.getByRole("button", { name: "View prompt" }));
    expect(
      screen.queryByText(/approve unlocks once you have viewed it/i),
    ).toBeNull();
    expect(approve.getAttribute("aria-describedby")).toBeNull();
  });

  it("has a stable DOM id the wire-stage blocker links can target", () => {
    const event = makeEvent({ kind: "llm_prompt_template" });
    renderCard(event, { showAmend: false });
    const section = document.getElementById(`ack-card-${event.id}`);
    expect(section).not.toBeNull();
    expect(section?.getAttribute("data-testid")).toBe("acknowledgement-card");
  });
});

// ── Resolved-prompt rendering (elspeth-990f5ea562) ───────────────────────────

/** Composition state whose node-1 carries structured prompt parts. */
function makePromptPartsState(
  requirement: Record<string, unknown>,
  version = 5,
): CompositionState {
  return {
    ...makeCompositionState(version),
    nodes: [
      {
        id: "node-1",
        node_type: "transform",
        plugin: "llm",
        input: "rows",
        on_success: null,
        on_error: null,
        options: {
          prompt_template_parts: [
            { kind: "text", text: "Summarise " },
            { kind: "interpretation_ref", requirement_id: "req-1" },
            { kind: "text", text: " for an auditor." },
          ],
          interpretation_requirements: [requirement],
        },
      },
    ],
  };
}

const RESOLVED_REQUIREMENT = {
  id: "req-1",
  kind: "vague_term",
  user_term: "punchy",
  status: "resolved",
  draft: "short and direct",
  event_id: "evt-vague-1",
  accepted_value: "concise and neutral",
  accepted_artifact_hash: null,
  resolved_prompt_template_hash: null,
};

const PENDING_REQUIREMENT = {
  ...RESOLVED_REQUIREMENT,
  status: "pending",
  accepted_value: null,
};

/** The staging-time frozen draft: slot masked as 'pending interpretation'. */
const MASKED_DRAFT = "Summarise pending interpretation for an auditor.";

describe("AcknowledgementCard — resolved-prompt rendering", () => {
  it("shows the accepted sibling value (not the frozen 'pending interpretation' mask)", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
      {
        showAmend: false,
        compositionState: makePromptPartsState(RESOLVED_REQUIREMENT),
      },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const region = screen.getByRole("region", {
      name: /prompt template review/i,
    });
    const mark = region.querySelector("mark.ack-card-prompt-slot--resolved");
    // The slot carries a visually-hidden "accepted value: " prefix so the
    // resolved/pending distinction is not CSS-only (WCAG 1.3.1); textContent
    // therefore includes it even though only the value is visible.
    expect(mark?.textContent).toBe("accepted value: concise and neutral");
    // The PRIMARY render carries the substitution; the frozen mask survives
    // only inside the secondary 'View original template' disclosure.
    const primaryPre = region.querySelector("pre.ack-card-prompt-pre");
    expect(primaryPre?.textContent).toBe(
      "Summarise accepted value: concise and neutral for an auditor.",
    );
    expect(primaryPre?.textContent).not.toContain("pending interpretation");
    // Fully resolved: no pending note, and the legend keys only the tint
    // that is actually on screen.
    expect(screen.queryByText(/pending values are drafts/i)).toBeNull();
    expect(screen.getByText("Accepted value")).toBeTruthy();
    expect(
      screen.queryByText(/pending value — awaiting its own review/i),
    ).toBeNull();
  });

  it("highlights a still-pending slot with its draft and the pending note", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
      {
        showAmend: false,
        compositionState: makePromptPartsState(PENDING_REQUIREMENT),
      },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const region = screen.getByRole("region", {
      name: /prompt template review/i,
    });
    const mark = region.querySelector("mark.ack-card-prompt-slot--pending");
    // Visually-hidden "pending value: " prefix — see the resolved-slot test.
    expect(mark?.textContent).toBe("pending value: short and direct");
    expect(screen.getByText(/pending values are drafts/i)).toBeTruthy();
    // All-pending: only the pending tint is keyed.
    expect(
      screen.getByText("Pending value — awaiting its own review"),
    ).toBeTruthy();
    expect(screen.queryByText("Accepted value")).toBeNull();
  });

  // ── Mixed prompt: the case the old caption got wrong ──────────────────────
  //
  // ux-review 2026-08-13 (UX-2): resolved AND pending slots both render as
  // <mark>, differing only by CSS class, and the note said "Highlighted
  // values await their own interpretation reviews" — false for every resolved
  // slot.  On a mixed prompt (one accepted slot, one still pending: the
  // normal state part-way through a review queue) an operator either
  // discounted a value they had already attested or read a draft as settled.
  // The visible legend now matches the sr-only per-slot prefixes, which had
  // this right all along.
  it("keys BOTH tints and scopes the note to pending on a mixed prompt", async () => {
    const user = userEvent.setup();
    const mixedState: CompositionState = {
      ...makeCompositionState(5),
      nodes: [
        {
          id: "node-1",
          node_type: "transform",
          plugin: "llm",
          input: "rows",
          on_success: null,
          on_error: null,
          options: {
            prompt_template_parts: [
              { kind: "text", text: "Summarise " },
              { kind: "interpretation_ref", requirement_id: "req-1" },
              { kind: "text", text: " complaints at " },
              { kind: "interpretation_ref", requirement_id: "req-2" },
              { kind: "text", text: " severity." },
            ],
            interpretation_requirements: [
              RESOLVED_REQUIREMENT,
              { ...PENDING_REQUIREMENT, id: "req-2", draft: "high" },
            ],
          },
        },
      ],
    };
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
      { showAmend: false, compositionState: mixedState },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const region = screen.getByRole("region", {
      name: /prompt template review/i,
    });
    // Both tints are on screen…
    expect(
      region.querySelectorAll("mark.ack-card-prompt-slot--resolved"),
    ).toHaveLength(1);
    expect(
      region.querySelectorAll("mark.ack-card-prompt-slot--pending"),
    ).toHaveLength(1);
    // …so BOTH legend items render, and the distinction is carried by TEXT
    // (the swatches are aria-hidden decoration, stripped under forced-colors).
    expect(screen.getByText("Accepted value")).toBeTruthy();
    expect(
      screen.getByText("Pending value — awaiting its own review"),
    ).toBeTruthy();
    // The note's subject is the PENDING tint alone — never "Highlighted
    // values", which was true of the resolved slot too.
    const note = screen.getByText(/drafts from their own reviews/i);
    expect(note.textContent).toContain("Pending values");
    expect(note.textContent).not.toContain("Highlighted values");
  });

  it("keys nothing when the prompt has no interpretation slots at all", async () => {
    // A legend for tints that are not on screen would be noise, and the
    // one-way invariant runs the same direction as the control naming:
    // never key a distinction the render does not make.
    const user = userEvent.setup();
    renderCard(
      makeEvent({
        kind: "llm_prompt_template",
        llm_draft: "Summarise the row for an auditor.",
      }),
      { showAmend: false, compositionState: null },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    expect(screen.queryByText("Accepted value")).toBeNull();
    expect(
      screen.queryByText("Pending value — awaiting its own review"),
    ).toBeNull();
    expect(screen.queryByText(/pending values are drafts/i)).toBeNull();
  });

  // ── Fallback visibility (code-review 2026-08-13) ──────────────────────────
  //
  // promptTemplateDisplay's `usedFallback` was computed on every return path
  // and read by nothing.  On a card whose entire purpose is showing what
  // actually runs, a degraded render that looks identical to the resolved one
  // is the wrong silence — so the flag now drives a visible note.
  it("says so when the structured parts could not be rendered", async () => {
    const user = userEvent.setup();
    const malformedState: CompositionState = {
      ...makeCompositionState(5),
      nodes: [
        {
          id: "node-1",
          node_type: "transform",
          plugin: "llm",
          input: "rows",
          on_success: null,
          on_error: null,
          options: {
            // Malformed parts → the resolver falls back to prompt_template.
            prompt_template_parts: "not-a-list",
            interpretation_requirements: [],
            prompt_template: "Summarise the row for an auditor.",
          },
        },
      ],
    };
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
      { showAmend: false, compositionState: malformedState },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    expect(
      screen.getByText(/showing the stored prompt template as-is/i),
    ).toBeTruthy();
  });

  it("shows no fallback note when the structured parts DID render", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
      {
        showAmend: false,
        compositionState: makePromptPartsState(RESOLVED_REQUIREMENT),
      },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    expect(
      screen.queryByText(/showing the stored prompt template as-is/i),
    ).toBeNull();
  });

  it("names each slot's review status in visually-hidden text (not CSS-only)", async () => {
    // The resolved-vs-pending distinction is a status fact on an approval
    // surface; background/border on <mark> alone is imperceivable to a
    // screen-reader user, so each slot carries a .visually-hidden prefix
    // ("accepted value: " / "pending value: ") inside the mark.
    const user = userEvent.setup();
    for (const [requirement, slotKind, prefix] of [
      [RESOLVED_REQUIREMENT, "resolved", "accepted value: "],
      [PENDING_REQUIREMENT, "pending", "pending value: "],
    ] as const) {
      const { unmount } = renderCard(
        makeEvent({ kind: "llm_prompt_template", llm_draft: MASKED_DRAFT }),
        {
          showAmend: false,
          compositionState: makePromptPartsState(requirement),
        },
      );
      await user.click(screen.getByRole("button", { name: "View prompt" }));
      const mark = document.querySelector(
        `mark.ack-card-prompt-slot--${slotKind}`,
      );
      const hidden = mark?.querySelector("span.visually-hidden");
      expect(hidden?.textContent).toBe(prefix);
      unmount();
    }
  });

  it("offers 'View original template' with the frozen draft only when it differs", async () => {
    const user = userEvent.setup();
    const event = makeEvent({
      kind: "llm_prompt_template",
      llm_draft: MASKED_DRAFT,
    });
    const { unmount } = renderCard(event, {
      showAmend: false,
      compositionState: makePromptPartsState(RESOLVED_REQUIREMENT),
    });
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const disclosure = screen.getByText("View original template");
    expect(disclosure.closest("details")?.textContent).toContain(MASKED_DRAFT);
    unmount();

    // Fallback path (no composition state): the render IS the frozen draft,
    // so no secondary disclosure appears.
    renderCard(event, { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    expect(screen.getByText(MASKED_DRAFT)).toBeTruthy();
    expect(screen.queryByText("View original template")).toBeNull();
  });

  it("re-renders with fresh substitutions when a sibling resolve updates the state prop", async () => {
    const user = userEvent.setup();
    const event = makeEvent({
      kind: "llm_prompt_template",
      llm_draft: MASKED_DRAFT,
    });
    const { rerender } = render(
      <AcknowledgementCard
        event={event}
        sessionId="sess-1"
        stepLabel="Summarise"
        compositionState={makePromptPartsState(PENDING_REQUIREMENT, 5)}
        showAmend={false}
      />,
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    // Read the slot through its class + textContent, NOT getByText: the
    // visually-hidden prefix is a child <span>, and testing-library's text
    // matcher only joins an element's DIRECT text nodes, so no single element
    // ever "has" the combined string.
    function slotText(kind: "pending" | "resolved"): string | null {
      const mark = document.querySelector(`mark.ack-card-prompt-slot--${kind}`);
      return mark === null ? null : mark.textContent;
    }
    expect(slotText("pending")).toBe("pending value: short and direct");
    expect(slotText("resolved")).toBeNull();

    rerender(
      <AcknowledgementCard
        event={event}
        sessionId="sess-1"
        stepLabel="Summarise"
        compositionState={makePromptPartsState(RESOLVED_REQUIREMENT, 6)}
        showAmend={false}
      />,
    );
    expect(slotText("resolved")).toBe("accepted value: concise and neutral");
    expect(slotText("pending")).toBeNull();
  });
});

// ── Accessibility / focus ────────────────────────────────────────────────────

describe("AcknowledgementCard — a11y", () => {
  it("does NOT move focus on mount (announce-don't-steal)", () => {
    renderCard(makeEvent());
    const accept = screen.getByRole("button", {
      name: /acknowledge the llm's interpretation of cool/i,
    });
    expect(document.activeElement).not.toBe(accept);
  });

  it("is a region labelled by the title and uses real <button>s", () => {
    renderCard(makeEvent());
    expect(screen.getByRole("region", { name: /interpretation/i })).toBeTruthy();
    const accept = screen.getByRole("button", {
      name: /acknowledge the llm's interpretation of cool/i,
    });
    expect(accept.tagName).toBe("BUTTON");
  });

  it("amend button's accessible name starts with its visible label verb (WCAG 2.5.3)", () => {
    renderCard(makeEvent());
    const amend = screen.getByRole("button", {
      name: /change the interpretation of cool/i,
    });
    // Visible label is the shared constant ("Change…", U+2026); the
    // accessible name must begin with the same verb so speech-input users
    // can say what they see (elspeth-0a9f77dd75 label-in-name fix — the
    // old aria-label said "Edit the interpretation of …").
    expect(amend.textContent).toBe(ACKNOWLEDGEMENT_AMEND_LABEL);
    expect(amend.getAttribute("aria-label")).toMatch(/^Change /);
  });
});
