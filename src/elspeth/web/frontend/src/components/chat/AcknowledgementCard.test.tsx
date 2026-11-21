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
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  AcknowledgementCard,
  parseDataContractDraft,
} from "./AcknowledgementCard";
import {
  ACKNOWLEDGEMENT_AMEND_LABEL,
  ACKNOWLEDGEMENT_APPROVE_LABEL,
  ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
} from "./acknowledgementLabels";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
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
  getPromptReviewSnapshot: vi.fn(),
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
    approved_prompt_artifact_hash: null,
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

const DEFAULT_ANCHOR = "d".repeat(64);

/** A stored inline review snapshot for evt-1, as GET .../prompt returns it. */
function promptSnapshot(
  authoredSurface: Record<string, unknown>,
  anchor = DEFAULT_ANCHOR,
): Awaited<ReturnType<typeof api.getPromptReviewSnapshot>> {
  return {
    version: 1, session_id: "sess-1", node_id: "node-1", event_id: "evt-1",
    authored_surface: authoredSurface, bindings: [], anchor,
    effective_artifact_hash: null, is_live_review: true,
  };
}

/**
 * Render a card. A prompt card loads its stored review snapshot; unless the
 * test configured the mock itself, the snapshot's surface is `promptSurface`
 * (default: the event's draft as the template).
 */
function renderCard(
  event: InterpretationEvent,
  props: Partial<ComponentProps<typeof AcknowledgementCard>> & { promptSurface?: Record<string, unknown> } = {},
) {
  if (event.kind === "llm_prompt_template" && vi.mocked(api.getPromptReviewSnapshot).getMockImplementation() === undefined) {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(
      promptSnapshot(props.promptSurface ?? { prompt_template: event.llm_draft ?? "" }),
    );
  }
  return render(
    <AcknowledgementCard
      event={event}
      sessionId="sess-1"
      stepLabel={props.stepLabel ?? "Summarise"}
      showAmend={props.showAmend ?? event.kind === "vague_term"}
      onResolved={props.onResolved}
    />,
  );
}

beforeEach(() => {
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({ compositionState: null });
  vi.mocked(api.resolveInterpretation).mockReset();
  vi.mocked(api.optOutOfInterpretations).mockReset();
  vi.mocked(api.listInterpretationEvents).mockReset();
  vi.mocked(api.getPromptReviewSnapshot).mockReset();
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

  it("pipeline_decision: strips a trailing [user_term: ...] annotation from the draft", () => {
    // Backward compatibility: events persisted before the register-leak fix
    // (elspeth-9665dcca32) carry the raw registry-key trailer inside the
    // draft body. The card is a user surface — the annotation never renders.
    renderCard(
      makeEvent({
        kind: "pipeline_decision",
        llm_draft:
          "Recommend inserting a prompt-injection shield. Continuing without it is allowed. " +
          "[user_term: prompt_injection_shield_recommendation]",
      }),
      { stepLabel: "Invest LLM", showAmend: false },
    );
    expect(screen.getByText(/recommend inserting a prompt-injection shield/i)).toBeTruthy();
    expect(screen.queryByText(/user_term/)).toBeNull();
    expect(screen.queryByText(/prompt_injection_shield_recommendation/)).toBeNull();
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

describe("AcknowledgementCard — source data contract", () => {
  function makeDataContractEvent(
    draft: Record<string, unknown>,
  ): InterpretationEvent {
    return makeEvent({
      kind: "source_data_contract",
      affected_node_id: "source",
      user_term: "source_data_contract",
      llm_draft: JSON.stringify(draft),
    });
  }

  it("renders the demanded fields, the commitment framing, and the fail-closed consequence", () => {
    renderCard(
      makeDataContractEvent({
        contract_version: 2,
        kind: "source_data_contract",
        demanded_fields: ["colour", "size"],
        sample_header: ["colour", "extra"],
        missing_from_sample: ["size"],
      }),
      { showAmend: false },
    );
    expect(screen.getByText("Data contract")).toBeTruthy();
    expect(screen.getByText("colour")).toBeTruthy();
    expect(screen.getByText("size")).toBeTruthy();
    // Commitment wording: a promise about future rows, not the sample file.
    expect(
      screen.getByText(/whatever you feed this pipeline/i),
    ).toBeTruthy();
    // A broken acknowledged producer guarantee is a Tier-1 declaration
    // violation, distinct from Tier-3 source validation quarantine.
    expect(
      screen.getByText(/both its data and emitted schema contract/i),
    ).toBeTruthy();
    expect(screen.getByText(/the run stops/i)).toBeTruthy();
    expect(screen.getByText(/source data-contract failure/i)).toBeTruthy();
    expect(screen.queryByText(/the run continues/i)).toBeNull();
    expect(
      screen.getByRole("button", {
        name: /acknowledge the source data contract/i,
      }),
    ).toBeTruthy();
  });

  it("warns per demanded field the sample does not show", () => {
    renderCard(
      makeDataContractEvent({
        contract_version: 2,
        kind: "source_data_contract",
        demanded_fields: ["colour", "size"],
        sample_header: ["colour", "extra"],
        missing_from_sample: ["size"],
      }),
      { showAmend: false },
    );
    const warnings = screen.getAllByRole("alert");
    expect(
      warnings.some((el) =>
        /your data doesn't appear to have this/i.test(el.textContent ?? ""),
      ),
    ).toBe(true);
    // The satisfied field carries no warning: exactly one alert for 'size'.
    expect(
      warnings.filter((el) =>
        /your data doesn't appear to have this/i.test(el.textContent ?? ""),
      ),
    ).toHaveLength(1);
  });

  it("notes when no sample was available", () => {
    renderCard(
      makeDataContractEvent({
        contract_version: 2,
        kind: "source_data_contract",
        demanded_fields: ["colour"],
        sample_header: null,
        missing_from_sample: [],
      }),
      { showAmend: false },
    );
    expect(screen.getByText(/no sample of this source's data/i)).toBeTruthy();
  });

  it("falls back to the raw payload when the draft cannot be parsed", () => {
    renderCard(
      makeEvent({
        kind: "source_data_contract",
        affected_node_id: "source",
        user_term: "source_data_contract",
        llm_draft: "not json at all",
      }),
      { showAmend: false },
    );
    expect(
      screen.getByText(/could not be parsed/i),
    ).toBeTruthy();
  });

  it.each([
    ["legacy version", { contract_version: 1, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null, missing_from_sample: [] }],
    ["wrong kind", { contract_version: 2, kind: "invented_source", demanded_fields: ["colour"], sample_header: null, missing_from_sample: [] }],
    ["missing field", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null }],
    ["malformed sample", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: 42, missing_from_sample: [] }],
    ["empty demand", { contract_version: 2, kind: "source_data_contract", demanded_fields: [], sample_header: null, missing_from_sample: [] }],
    ["unsorted demand", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["size", "colour"], sample_header: null, missing_from_sample: [] }],
    ["duplicate demand", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour", "colour"], sample_header: null, missing_from_sample: [] }],
    ["sample miss omitted", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: [], missing_from_sample: [] }],
    ["no sample has miss", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null, missing_from_sample: ["colour"] }],
    ["malformed misses", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null, missing_from_sample: [1] }],
    ["miss is not demanded", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null, missing_from_sample: ["size"] }],
    ["extra field", { contract_version: 2, kind: "source_data_contract", demanded_fields: ["colour"], sample_header: null, missing_from_sample: [], extra: true }],
  ])("rejects %s instead of rendering a degraded acknowledgement", (_label, payload) => {
    expect(parseDataContractDraft(JSON.stringify(payload))).toBeNull();
  });
});

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
    const warning = screen.getByRole("status");
    expect(warning).toHaveTextContent("Shorten this by about 8 characters to fit the 8 KB limit.");
    // Exact figures reachable by every audience, not hover-only.
    expect(warning).toHaveTextContent("(8200 bytes; the maximum is 8192 bytes.)");
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });
});

// ── Error mapping ────────────────────────────────────────────────────────────

describe("AcknowledgementCard — error mapping", () => {
  it("coded 409 reports an already-resolved review without inventing another tab", async () => {
    const user = userEvent.setup();
    vi.mocked(api.resolveInterpretation).mockRejectedValue({
      ...makeApiError(409),
      error_type: "interpretation_already_resolved",
    });
    renderCard(makeEvent());
    await user.click(
      screen.getByRole("button", {
        name: /acknowledge the llm's interpretation of cool/i,
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already resolved/i);
    expect(alert.textContent).not.toMatch(/another tab/i);
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
  it("loads and shows the full escaped file text before allowing approval", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "File-backed prompt pending review" });
    const marker = { blob_ref: "blob-1", mode: "inline_content", sha256: "a".repeat(64) };
    const text = "  <script> & literal\n\nsecond line  ";
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue({
      version: 1, session_id: "sess-1", node_id: "node-1", event_id: event.id,
      authored_surface: { prompt_template: marker },
      bindings: [{ field_path: "prompt_template", blob_id: "blob-1", content_sha256: "a".repeat(64),
        encoding: "utf-8", byte_length: text.length, filename: "prompt.txt", creation_modality: "llm_generated",
        created_by: "assistant", provenance: {}, text }],
      anchor: "b".repeat(64), effective_artifact_hash: "c".repeat(64), is_live_review: true,
    });
    renderCard(event);
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region.querySelector("pre.ack-card-prompt-pre")?.textContent).toBe(text));
    // The file sits under the prompt's heading with no raw slot name.
    expect(region.querySelector(".ack-card-prompt-file")).toHaveTextContent("File: prompt.txt");
    expect(region).not.toHaveTextContent("(prompt_template)");
    expect(region.innerHTML).not.toContain("<script>");
    expect(region).not.toHaveTextContent("not text");
    expect(approve).not.toHaveAttribute("aria-disabled");
    expect(api.getPromptReviewSnapshot).toHaveBeenCalledWith("sess-1", event.id, expect.any(AbortSignal));
  });
  it("puts each file under its own prompt heading, with readable labels", async () => {
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "draft" });
    const marker = (id: string) => ({ blob_ref: id, mode: "inline_content", sha256: "a".repeat(64) });
    const bound = (field_path: string, blob_id: string, filename: string, text: string) => ({ field_path, blob_id, content_sha256: "a".repeat(64),
      encoding: "utf-8" as const, byte_length: text.length, filename, creation_modality: "verbatim", created_by: "user", provenance: {}, text });
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue({
      version: 1, session_id: "sess-1", node_id: "node-1", event_id: event.id,
      authored_surface: { system_prompt: marker("sys"), prompt_template: marker("user") },
      bindings: [bound("system_prompt", "sys", "risk_system_prompt.txt", "System text."), bound("prompt_template", "user", "risk_user_prompt.txt", "User text.")],
      anchor: "b".repeat(64), effective_artifact_hash: null, is_live_review: true,
    });
    renderCard(event, { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const region = await screen.findByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region.querySelectorAll(".ack-card-prompt-file")).toHaveLength(2));
    // Heading, its file, its text, in that order, for each prompt.
    const order = Array.from(region.querySelectorAll(".ack-card-prompt-role, .ack-card-prompt-file, pre.ack-card-prompt-pre"))
      .filter((element) => element.closest("details") === null).map((element) => element.textContent);
    expect(order).toEqual(["System prompt", "File: risk_system_prompt.txt", "System text.", "User prompt", "File: risk_user_prompt.txt", "User text."]);
    expect(region).not.toHaveTextContent("(system_prompt)");
    expect(region).not.toHaveTextContent("(prompt_template)");
  });

  it("unlocks approval after viewing all query templates when no shared template exists", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Shortened preview" });
    const queries = Array.from({ length: 6 }, (_, index) => ({
      name: `query_${index}`,
      template: `Instruction ${index}: ${"detail ".repeat(250)}end ${index}.`,
    }));
    renderCard(event, { promptSurface: { system_prompt: "Shared system instruction.", queries } });
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    await user.click(approve);
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("Shared system instruction."));
    const text = region.querySelector("pre.ack-card-prompt-pre")?.textContent ?? "";
    expect(text.length).toBeGreaterThan(8000);
    expect(text).toContain("System prompt (sent with every query):\nShared system instruction.");
    for (const query of queries) {
      expect(text).toContain(`Query '${query.name}':\n${query.template}\n`);
    }
    expect(approve).not.toHaveAttribute("aria-disabled");
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
    vi.mocked(api.resolveInterpretation).mockResolvedValue(makeResolveResponse(event));
    await user.click(approve);
    expect(api.resolveInterpretation).toHaveBeenCalledTimes(1);
  });

  /** Hold the snapshot request open until the test releases it. */
  function deferSnapshot(surface: Record<string, unknown>): () => Promise<void> {
    let release!: () => void;
    vi.mocked(api.getPromptReviewSnapshot).mockReturnValue(
      new Promise((resolve) => { release = () => resolve(promptSnapshot(surface)); }),
    );
    return async () => { await act(async () => { release(); }); };
  }

  it("does not approve a bounded draft while the reviewed snapshot is loading", async () => {
    const user = userEvent.setup();
    deferSnapshot({ prompt_template: "Complete instruction" });
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Shortened preview" }));
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /approve the llm prompt template/i })).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByText("Shortened preview")).toBeNull();
  });

  it("requires viewing the full prompt after a collapsed preview loads", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Shortened preview" });
    const release = deferSnapshot({ prompt_template: "Complete instruction previously omitted" });
    renderCard(event);
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await user.click(screen.getByRole("button", { name: "Hide prompt" }));
    await release();
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByText("Complete instruction previously omitted")).toBeNull();
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    expect(screen.getByText("Complete instruction previously omitted")).toBeVisible();
    expect(approve).not.toHaveAttribute("aria-disabled");
    vi.mocked(api.resolveInterpretation).mockResolvedValue(makeResolveResponse(event));
    await user.click(approve);
    expect(api.resolveInterpretation).toHaveBeenCalledTimes(1);
  });

  it("unlocks when the full prompt loads into an open disclosure and stays unlocked after collapse", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Shortened preview" });
    const release = deferSnapshot({ prompt_template: "Complete instruction previously omitted" });
    renderCard(event);
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    await release();
    expect(screen.getByText("Complete instruction previously omitted")).toBeVisible();
    expect(approve).not.toHaveAttribute("aria-disabled");
    await user.click(screen.getByRole("button", { name: "Hide prompt" }));
    expect(approve).not.toHaveAttribute("aria-disabled");
  });

  it("shows both prompt roles before approval of a single-prompt LLM node", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({
        kind: "llm_prompt_template",
        llm_draft: "System prompt:\nYou are a careful reviewer.\n\nPrompt template:\nSummarise {{ row.text }}.",
      }),
      { showAmend: false, promptSurface: { system_prompt: "You are a careful reviewer.", prompt_template: "Summarise {{ row.text }}." } },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("System prompt"));
    expect(region).toHaveTextContent("You are a careful reviewer.");
    expect(region).toHaveTextContent("User prompt");
    expect(region).toHaveTextContent("Summarise {{ row.text }}.");
  });

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

  // Operator report 2026-08-16: pre-view, "View prompt" was the card's ONLY
  // live control yet the only one not drawn as one — users did not know how
  // to clear the card. The --next modifier hands it the filled active
  // treatment exactly while Approve is gated, and hands the emphasis back
  // (class dropped, permanently — viewing latches) once viewed.
  it("carries the active-control emphasis exactly while Approve is gated", async () => {
    const user = userEvent.setup();
    renderCard(
      makeEvent({
        kind: "llm_prompt_template",
        llm_draft: "Summarise {{ row.body }}.",
      }),
      { showAmend: false },
    );

    const toggle = screen.getByRole("button", { name: "View prompt" });
    expect(toggle.classList.contains("ack-card-view-toggle--next")).toBe(true);

    await user.click(toggle);
    expect(toggle.classList.contains("ack-card-view-toggle--next")).toBe(false);

    // Collapsing does not re-gate Approve, so the emphasis must not return.
    await user.click(screen.getByRole("button", { name: "Hide prompt" }));
    expect(toggle.classList.contains("ack-card-view-toggle--next")).toBe(false);
  });

  it("never applies the gated emphasis to a non-prompt View expander", () => {
    renderCard(
      makeEvent({
        kind: "invented_source",
        llm_draft: JSON.stringify({ rows: "x".repeat(200) }),
      }),
      { showAmend: false },
    );

    const toggle = screen.getByRole("button", { name: "View" });
    expect(toggle.classList.contains("ack-card-view-toggle--next")).toBe(false);
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

    const note = await screen.findByText(/approve unlocks once you have viewed it/i);
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

// ── Snapshot rendering ───────────────────────────────────────────────────────

/** The staging-time bounded draft. */
const MASKED_DRAFT = "Summarise pending interpretation for an auditor.";

describe("AcknowledgementCard — snapshot rendering", () => {
  it("offers 'View original template' with the frozen draft only when it differs", async () => {
    const user = userEvent.setup();
    const event = makeEvent({
      kind: "llm_prompt_template",
      llm_draft: MASKED_DRAFT,
    });
    const { unmount } = renderCard(event, {
      showAmend: false,
      promptSurface: { prompt_template: "Summarise concise and neutral for an auditor." },
    });
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const disclosure = await screen.findByText("View original template");
    expect(disclosure.closest("details")?.textContent).toContain(MASKED_DRAFT);
    unmount();

    // The snapshot text equals the draft: no secondary disclosure.
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(promptSnapshot({ prompt_template: MASKED_DRAFT }));
    renderCard(event, { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    expect(await screen.findByText(MASKED_DRAFT)).toBeTruthy();
    expect(screen.queryByText("View original template")).toBeNull();
  });

  it("multi-query card: renders every query and the node-level template, in a scrolling region", async () => {
    // The bounded staging-time draft omits queries the review anchor covers;
    // the card must show the live, complete surface instead.
    const user = userEvent.setup();
    const queries: Record<string, unknown> = {};
    for (let index = 0; index < 40; index += 1) {
      queries[`q_${index}`] = {
        template: `Template ${index}: ${"words ".repeat(50)}done ${index}.`,
      };
    }
    queries.shared = {};
    const frozenDraft =
      "Multi-query LLM node: for every row the model receives one call per query below.\n\nQueries not listed in this draft: the last 18 of 41; the review attests their names and templates in full.";
    renderCard(
      makeEvent({ kind: "llm_prompt_template", llm_draft: frozenDraft }),
      {
        showAmend: false,
        promptSurface: { prompt_template: "Summarise concise and neutral.", system_prompt: "Reply briefly.", queries },
      },
    );
    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const region = screen.getByRole("region", {
      name: /prompt template review/i,
    });
    await waitFor(() => expect(region).toHaveTextContent("Reply briefly."));
    // A long surface scrolls inside the region rather than growing the card.
    expect(region.style.maxHeight).toBe("16rem");
    expect(region.style.overflow).toBe("auto");
    const primaryPre = region.querySelector("pre.ack-card-prompt-pre");
    const text = primaryPre?.textContent ?? "";
    expect(text).toContain("System prompt (sent with every query):\nReply briefly.\n");
    for (let index = 0; index < 40; index += 1) {
      expect(text).toContain(
        `Query 'q_${index}':\nTemplate ${index}: ${"words ".repeat(50)}done ${index}.\n`,
      );
    }
    expect(text).toContain(
      "Query 'shared': uses the node-level prompt_template (below).",
    );
    expect(text).toContain(
      "Node-level prompt_template, used by queries without their own template: shared\nSummarise concise and neutral.",
    );
    expect(text).not.toContain("not listed");
    // The frozen draft stays reachable only behind the secondary disclosure.
    const disclosure = screen.getByText("View original template");
    expect(disclosure.closest("details")?.textContent).toContain(
      "Queries not listed in this draft",
    );
  });
});

describe("AcknowledgementCard — multi-query prompt without a shared template", () => {
  // Staging report 2026-11-16: every query supplies its own template and the
  // optional node-level prompt_template is omitted. The card must render the
  // live surface and let the user approve after viewing it.
  function multiQuerySurface(queries: unknown[]): Record<string, unknown> {
    return { system_prompt: "Reply briefly.", queries };
  }

  it("shows every per-query template and unlocks Approve after View prompt", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded staging-time preview." });
    vi.mocked(api.resolveInterpretation).mockResolvedValue(makeResolveResponse(event));
    renderCard(event, {
      showAmend: false,
      promptSurface: multiQuerySurface([
        { name: "first", template: "Summarise {{ row.text }}." },
        { name: "second", template: "List themes in {{ row.text }}." },
      ]),
    });
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve.getAttribute("aria-disabled")).toBe("true");

    await user.click(screen.getByRole("button", { name: "View prompt" }));

    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("Reply briefly."));
    const text = region.querySelector("pre.ack-card-prompt-pre")?.textContent ?? "";
    expect(text).toContain("System prompt (sent with every query):\nReply briefly.\n");
    expect(text).toContain("Query 'first':\nSummarise {{ row.text }}.\n");
    expect(text).toContain("Query 'second':\nList themes in {{ row.text }}.\n");
    expect(screen.queryByText(/the complete prompt is not available/i)).toBeNull();
    expect(approve.getAttribute("aria-disabled")).not.toBe("true");
    expect(api.resolveInterpretation).not.toHaveBeenCalled();

    await user.click(approve);
    await waitFor(() => expect(api.resolveInterpretation).toHaveBeenCalledTimes(1));
  });

  it("keeps Approve unavailable when a query needs the missing shared template", async () => {
    const user = userEvent.setup();
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded staging-time preview." }), {
      showAmend: false,
      promptSurface: multiQuerySurface([{ name: "first", template: "A." }, { name: "second" }]),
    });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await waitFor(() => expect(screen.getByRole("region", { name: /prompt template review/i })).toHaveTextContent("Query 'second'"));
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText(/the complete prompt is not available/i)).toBeTruthy();
    await user.click(approve);
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });
});

// ── Reviewed snapshot binding ────────────────────────────────────────────────
//
// The card shows the server's stored review snapshot and approves THAT
// snapshot's anchor; the browser's compositionState can be stale (a second
// tab, a failed state read) and must never decide what the user approves.

const ANCHOR_1 = "1".repeat(64);
const ANCHOR_2 = "2".repeat(64);

function inlineSnapshot(
  authoredSurface: Record<string, unknown>,
  anchor: string,
  eventId = "evt-1",
): Awaited<ReturnType<typeof api.getPromptReviewSnapshot>> {
  return {
    version: 1, session_id: "sess-1", node_id: "node-1", event_id: eventId,
    authored_surface: authoredSurface, bindings: [], anchor,
    effective_artifact_hash: null, is_live_review: true,
  };
}

function staleBrowserState(): CompositionState {
  return {
    ...makeCompositionState(5),
    nodes: [{ id: "node-1", node_type: "transform", plugin: "llm", input: "rows", on_success: null, on_error: null,
      options: { system_prompt: "STALE TAB SYSTEM", prompt_template: "STALE TAB TEMPLATE" } }],
  };
}

describe("AcknowledgementCard — reviewed snapshot binding", () => {
  it("shows the stored snapshot, not a stale browser state, and approves that snapshot's anchor", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" });
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(
      inlineSnapshot({ system_prompt: "SERVER SYSTEM", prompt_template: "SERVER TEMPLATE" }, ANCHOR_1),
    );
    vi.mocked(api.resolveInterpretation).mockResolvedValue(makeResolveResponse(event));
    // The stale tab's state sits where any browser-state read would find it.
    useSessionStore.setState({ compositionState: staleBrowserState() });
    renderCard(event, { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("SERVER TEMPLATE"));
    expect(region).toHaveTextContent("SERVER SYSTEM");
    expect(region).not.toHaveTextContent("STALE TAB");
    await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));
    await waitFor(() => expect(api.resolveInterpretation).toHaveBeenCalledTimes(1));
    expect(api.resolveInterpretation).toHaveBeenCalledWith(
      "sess-1", event.id, { choice: "accepted_as_drafted", reviewed_anchor: ANCHOR_1 },
    );
  });

  it("renders an inline card from its snapshot even with no browser state", async () => {
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" });
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(
      inlineSnapshot({ prompt_template: "Inline snapshot instruction." }, ANCHOR_1),
    );
    useSessionStore.setState({ compositionState: null });
    renderCard(event, { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    await waitFor(() => expect(screen.getByRole("region", { name: /prompt template review/i }))
      .toHaveTextContent("Inline snapshot instruction."));
    expect(screen.getByRole("button", { name: /approve the llm prompt template/i })).not.toHaveAttribute("aria-disabled");
  });

  it.each(["prompt_review_stale", "prompt_review_snapshot_missing"])(
    "on a 409 %s re-fetches, re-renders, announces the change and does not approve",
    async (code) => {
      const user = userEvent.setup();
      const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" });
      vi.mocked(api.getPromptReviewSnapshot)
        .mockResolvedValueOnce(inlineSnapshot({ prompt_template: "FIRST TEXT" }, ANCHOR_1))
        .mockResolvedValueOnce(inlineSnapshot({ prompt_template: "SECOND TEXT" }, ANCHOR_2));
      vi.mocked(api.resolveInterpretation).mockRejectedValue({ ...makeApiError(409, "changed"), error_type: code });
      renderCard(event, { showAmend: false });
      await user.click(screen.getByRole("button", { name: "View prompt" }));
      await waitFor(() => expect(screen.getByRole("region", { name: /prompt template review/i })).toHaveTextContent("FIRST TEXT"));
      await user.click(screen.getByRole("button", { name: /approve the llm prompt template/i }));
      await waitFor(() => expect(api.getPromptReviewSnapshot).toHaveBeenCalledTimes(2));
      expect(await screen.findByText(/this prompt changed since you opened it/i)).toBeInTheDocument();
      expect(screen.getByText(/this prompt changed since you opened it/i).closest("[role='status']")).not.toBeNull();
      // The new text is shown only once the user opens it again: approval
      // re-gates, and nothing is approved on the user's behalf.
      const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
      expect(approve).toHaveAttribute("aria-disabled", "true");
      await user.click(screen.getByRole("button", { name: "View prompt" }));
      await waitFor(() => expect(screen.getByRole("region", { name: /prompt template review/i })).toHaveTextContent("SECOND TEXT"));
      expect(api.resolveInterpretation).toHaveBeenCalledTimes(1);
      expect(useInterpretationEventsStore.getState().resolvedBySession["sess-1"] ?? []).toHaveLength(0);
    },
  );

  it.each([
    ["a structured prompt (stored with the slot wording)", "Summarise pending interpretation for an auditor."],
    ["a legacy placeholder", "Summarise {{interpretation:punchy}} for an auditor."],
  ])("shows readable text at an interpretation slot in %s, and explains it", async (_case, template) => {
    const user = userEvent.setup();
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({ prompt_template: template }, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" }), { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region.querySelector("pre.ack-card-prompt-pre")?.textContent)
      .toBe("Summarise pending interpretation for an auditor."));
    expect(region).not.toHaveTextContent("{{");
    expect(region).toHaveTextContent(
      "“pending interpretation” marks a term you review in its own card; the prompt runs with the value you accept there.",
    );
    // A snapshot without the frozen parts skeleton has no slots to mark: no
    // legend, no marks, no crash.
    expect(region.querySelector("mark")).toBeNull();
    expect(region.querySelector(".ack-card-prompt-legend")).toBeNull();
  });

  it("disables Approve with a reason when the snapshot cannot load", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getPromptReviewSnapshot).mockRejectedValue(makeApiError(404, "not found"));
    useSessionStore.setState({ compositionState: staleBrowserState() });
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" }), { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    const reason = await screen.findByText(/could not be loaded/i);
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve.getAttribute("aria-describedby")).toBe(reason.id);
    expect(screen.queryByText("STALE TAB TEMPLATE")).toBeNull();
    await user.click(approve);
    expect(api.resolveInterpretation).not.toHaveBeenCalled();
  });
});

// ── Slot status from the interpretation events (option d) ────────────────────
//
// A structured prompt's slots come from the snapshot's frozen
// prompt_template_parts. A slot frozen with event_id: null (its term was not
// accepted when the card was made; the server writes event_id only once the
// term resolves) renders as a span saying it is reviewed in its own card,
// tinted and labelled with that review's live STATUS from the interpretation
// events store. Its live TEXT (accepted value, or the value in
// compositionState) is never shown. A slot naming an event shows that
// event's approved definition: AcknowledgementCard.carriedTerms.test.tsx.

const SLOTTED_SURFACE = {
  prompt_template: "Summarise pending interpretation in a pending interpretation tone.",
  prompt_template_parts: [
    { kind: "text", text: "Summarise " },
    { kind: "interpretation_ref", requirement_id: "req-topic" },
    { kind: "text", text: " in a " },
    { kind: "interpretation_ref", requirement_id: "req-tone" },
    { kind: "text", text: " tone." },
  ],
  interpretation_requirements: [
    { id: "req-topic", event_id: null, user_term: "the key points" },
    { id: "req-tone", event_id: null, user_term: "punchy" },
  ],
};

describe("AcknowledgementCard prompt copying", () => {
  it("copies each frozen field, including structured compiled text and every query", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const surface = {
      ...SLOTTED_SURFACE,
      system_prompt: "  Système 🐈\n ",
      prompt_template: " \nCompiled λ\n ",
      queries: { first: { template: " First\n " }, second: { template: "\tSecond 🐈 " } },
    };
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot(surface, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "bounded draft" }), { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const region = await screen.findByRole("region", { name: /prompt template review/i });
    await within(region).findByRole("button", { name: "Copy system prompt" });
    const labels = ["Copy system prompt", "Copy user prompt template", "Copy prompt template for query first", "Copy prompt template for query second"];
    const values = [surface.system_prompt, surface.prompt_template, surface.queries.first.template, surface.queries.second.template];
    expect(labels.map((label) => within(region).getByRole("button", { name: label }).textContent)).toEqual(labels);
    for (const label of labels) fireEvent.click(within(region).getByRole("button", { name: label }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(4));
    expect(writeText.mock.calls.map((call) => call[0])).toEqual(values);
    expect(writeText.mock.calls[1][0]).not.toContain("interpretation slot");
  });

  it.each([
    ["distinct names", [{ name: "one", template: "A" }, { name: "two", template: "B" }], ["one", "two"]],
    ["repeated names", [{ name: "same", template: "A" }, { name: "same", template: "B" }], ["same", "same"]],
    ["named and unnamed collision", [{ name: "#1", template: "A" }, { template: "B" }], ["#1", "#1"]],
    ["literal position collision", [{ name: "same", template: "A" }, { name: "same", template: "B" }, { name: "same (position 1)", template: "C" }], ["same", "same", "same (position 1)"]],
  ])("gives every list query a unique positional label for %s and copies its value", async (_case, queries, names) => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({ prompt_template: "Display", queries }, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "bounded draft" }), { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const region = await screen.findByRole("region", { name: /prompt template review/i });
    await within(region).findByRole("button", { name: `Copy prompt template for query ${names[0]} (position 1)` });
    const buttons = within(region).getAllByRole("button", { name: /^Copy prompt template for query / });
    const labels = buttons.map((button) => button.getAttribute("aria-label"));
    expect(labels).toEqual(names.map((name, index) => `Copy prompt template for query ${name} (position ${index + 1})`));
    expect(new Set(labels).size).toBe(labels.length);
    buttons.forEach((button, index) => {
      expect(button).toHaveTextContent(labels[index]!);
      fireEvent.click(button);
    });
    await waitFor(() => expect(writeText.mock.calls.map((call) => call[0])).toEqual(queries.map((query) => query.template)));
  });

  it("omits copy controls for a file marker and a non-string query", async () => {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({
      system_prompt: { mode: "inline_content", blob_ref: "file-1" },
      prompt_template: "Visible text",
      queries: [{ name: "number", template: 42 }],
    }, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "bounded draft" }), { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const region = await screen.findByRole("region", { name: /prompt template review/i });
    await within(region).findByRole("button", { name: "Copy user prompt template" });
    expect(within(region).queryByRole("button", { name: "Copy system prompt" })).not.toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /Copy prompt template for query/ })).not.toBeInTheDocument();
  });

  it("lines the copy controls up in one row under the text, and adds no empty row when nothing can be copied", async () => {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({
      prompt_template: "Display", queries: [{ name: "a", template: "A" }, { name: "b", template: "B" }],
    }, ANCHOR_1));
    const { unmount } = renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "bounded draft" }), { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const region = await screen.findByRole("region", { name: /prompt template review/i });
    await within(region).findByRole("button", { name: "Copy user prompt template" });
    const row = region.querySelector(".ack-card-copy-actions");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getAllByRole("button").map((button) => button.textContent))
      .toEqual(["Copy user prompt template", "Copy prompt template for query a (position 1)", "Copy prompt template for query b (position 2)"]);
    unmount();
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({ system_prompt: { mode: "inline_content", blob_ref: "file-1" } }, ANCHOR_2));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "bounded draft" }), { showAmend: false });
    fireEvent.click(screen.getByRole("button", { name: "View prompt" }));
    const fileOnly = await screen.findByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(fileOnly.querySelector("pre")).not.toBeNull());
    expect(fileOnly.querySelector(".ack-card-copy-actions")).toBeNull();
  });
});

function vagueEvent(id: string, term: string, overrides: Partial<InterpretationEvent> = {}): InterpretationEvent {
  return makeEvent({ id, kind: "vague_term", user_term: term, llm_draft: `DRAFT FOR ${term}`, ...overrides });
}

function setSlotEvents(pending: InterpretationEvent[], resolved: InterpretationEvent[]) {
  useInterpretationEventsStore.setState({
    pendingBySession: { "sess-1": Object.fromEntries(pending.map((event) => [event.id, event])) },
    resolvedBySession: { "sess-1": resolved },
  });
}

/** The visible text of each slot span (the sr-only prefix removed). */
function visibleSlotTexts(region: HTMLElement): string[] {
  return Array.from(region.querySelectorAll("mark.ack-card-prompt-slot")).map((mark) => {
    const clone = mark.cloneNode(true) as HTMLElement;
    clone.querySelectorAll(".visually-hidden").forEach((hidden) => hidden.remove());
    return clone.textContent ?? "";
  });
}

async function openSlottedCard() {
  const user = userEvent.setup();
  vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot(SLOTTED_SURFACE, ANCHOR_1));
  renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" }), { showAmend: false });
  await user.click(screen.getByRole("button", { name: "View prompt" }));
  const region = screen.getByRole("region", { name: /prompt template review/i });
  await waitFor(() => expect(region.querySelectorAll("mark.ack-card-prompt-slot")).toHaveLength(2));
  return region;
}

describe("AcknowledgementCard — slot status from the interpretation events", () => {
  it("after a vague-term accept, the slot's status changes and the prompt text does not", async () => {
    const tone = vagueEvent("evt-tone", "punchy");
    const topic = vagueEvent("evt-topic", "the key points");
    setSlotEvents([topic, tone], []);
    const region = await openSlottedCard();
    expect(visibleSlotTexts(region)).toEqual([
      "[“the key points”, reviewed in its own card: pending]",
      "[“punchy”, reviewed in its own card: pending]",
    ]);
    const textPieces = () =>
      Array.from(region.querySelectorAll("pre.ack-card-prompt-pre > span")).map((span) => span.textContent);
    const before = textPieces();
    expect(before).toEqual(["Summarise ", " in a ", " tone."]);

    // The tone review is accepted (amended to a new value), and the live
    // composition state now carries that value in the prompt.
    act(() => {
      setSlotEvents([topic], [{ ...tone, choice: "amended", accepted_value: "LIVE VALUE vivid and short" }]);
      useSessionStore.setState({
        compositionState: {
          ...makeCompositionState(6),
          nodes: [{ id: "node-1", node_type: "transform", plugin: "llm", input: "rows", on_success: null, on_error: null,
            options: { prompt_template: "Summarise pending interpretation in a LIVE VALUE vivid and short tone." } }],
        },
      });
    });

    expect(visibleSlotTexts(region)).toEqual([
      "[“the key points”, reviewed in its own card: pending]",
      "[“punchy”, reviewed in its own card: accepted]",
    ]);
    expect(textPieces()).toEqual(before);
    expect(region).not.toHaveTextContent("LIVE VALUE");
    expect(region).not.toHaveTextContent("DRAFT FOR");
    expect(api.getPromptReviewSnapshot).toHaveBeenCalledTimes(1);
  });

  // At surfacing the server freezes event_id: null for a slot whose vague
  // term has not resolved yet (the usual case). The status is then found by
  // the card's node and the slot's user_term, and only from EXACTLY one
  // vague_term event; anything else is "status not shown here".
  const UNLINKED_SURFACE = {
    ...SLOTTED_SURFACE,
    interpretation_requirements: [
      { id: "req-topic", event_id: null, user_term: "the key points" },
      { id: "req-tone", event_id: null, user_term: "punchy" },
    ],
  };

  async function openUnlinkedCard() {
    const user = userEvent.setup();
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot(UNLINKED_SURFACE, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" }), { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region.querySelectorAll("mark.ack-card-prompt-slot")).toHaveLength(2));
    return region;
  }

  it("finds an unlinked slot's status by node and term: pending, then accepted once its card resolves", async () => {
    const topic = vagueEvent("evt-topic", "the key points");
    const tone = vagueEvent("evt-tone", "punchy");
    setSlotEvents([topic, tone], []);
    const region = await openUnlinkedCard();
    expect(visibleSlotTexts(region)).toEqual([
      "[“the key points”, reviewed in its own card: pending]",
      "[“punchy”, reviewed in its own card: pending]",
    ]);
    act(() => setSlotEvents([topic], [{ ...tone, choice: "amended", accepted_value: "LIVE VALUE vivid" }]));
    expect(visibleSlotTexts(region)).toEqual([
      "[“the key points”, reviewed in its own card: pending]",
      "[“punchy”, reviewed in its own card: accepted]",
    ]);
    expect(region).not.toHaveTextContent("LIVE VALUE");
  });

  it("does not show an unlinked slot as accepted when two vague reviews on the node share its term", async () => {
    setSlotEvents(
      [vagueEvent("evt-tone-2", "punchy")],
      [vagueEvent("evt-tone-1", "punchy", { choice: "accepted_as_drafted" })],
    );
    const region = await openUnlinkedCard();
    expect(visibleSlotTexts(region)[1]).toBe("[“punchy”, reviewed in its own card: status not shown here]");
  });

  it("does not match an unlinked slot to another node's review of the same term", async () => {
    setSlotEvents([], [vagueEvent("evt-tone", "punchy", { choice: "accepted_as_drafted", affected_node_id: "node-2" })]);
    const region = await openUnlinkedCard();
    expect(visibleSlotTexts(region)[1]).toBe("[“punchy”, reviewed in its own card: status not shown here]");
  });

  it("does not match an unlinked slot to a review that is not a vague term", async () => {
    setSlotEvents([], [makeEvent({ id: "evt-x", kind: "llm_model_choice", user_term: "punchy", choice: "accepted_as_drafted" })]);
    const region = await openUnlinkedCard();
    expect(visibleSlotTexts(region)[1]).toBe("[“punchy”, reviewed in its own card: status not shown here]");
  });

  it("keeps an inline card approvable, with its anchor, after a sibling vague term resolved first", async () => {
    // The server re-renders the live prompt_template when a sibling slot is
    // resolved, so the inline snapshot reports is_live_review: false, while
    // the skeleton (and the anchor the resolve checks) is unchanged and the
    // server accepts the approval.
    const user = userEvent.setup();
    const event = makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" });
    setSlotEvents([vagueEvent("evt-topic", "the key points")], [vagueEvent("evt-tone", "punchy", { choice: "accepted_as_drafted" })]);
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue({
      ...inlineSnapshot(SLOTTED_SURFACE, ANCHOR_1),
      is_live_review: false,
    });
    vi.mocked(api.resolveInterpretation).mockResolvedValue(makeResolveResponse(event));
    renderCard(event, { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(visibleSlotTexts(region)).toContain("[“punchy”, reviewed in its own card: accepted]"));
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).not.toHaveAttribute("aria-disabled");
    await user.click(approve);
    await waitFor(() => expect(api.resolveInterpretation).toHaveBeenCalledTimes(1));
    expect(api.resolveInterpretation).toHaveBeenCalledWith(
      "sess-1", event.id, { choice: "accepted_as_drafted", reviewed_anchor: ANCHOR_1 },
    );
  });

  it("keys exactly the statuses on screen in the legend, in words", async () => {
    setSlotEvents([vagueEvent("evt-topic", "the key points")], [vagueEvent("evt-tone", "punchy", { choice: "accepted_as_drafted" })]);
    const region = await openSlottedCard();
    const legend = region.querySelector(".ack-card-prompt-legend");
    expect(Array.from(legend?.querySelectorAll("li") ?? []).map((item) => item.textContent)).toEqual([
      "Accepted — its own review is approved",
      "Pending — awaiting its own review",
    ]);
    expect(region.querySelector("mark.ack-card-prompt-slot--accepted")).not.toBeNull();
    expect(region.querySelector("mark.ack-card-prompt-slot--pending")).not.toBeNull();
  });

  it("keys only the pending status when every slot is pending", async () => {
    setSlotEvents([vagueEvent("evt-topic", "the key points"), vagueEvent("evt-tone", "punchy")], []);
    const region = await openSlottedCard();
    expect(Array.from(region.querySelectorAll(".ack-card-prompt-legend li")).map((item) => item.textContent)).toEqual([
      "Pending — awaiting its own review",
    ]);
  });

  it("shows a slot whose review is not loaded as status unknown, never as accepted", async () => {
    setSlotEvents([], [vagueEvent("evt-tone", "punchy", { choice: "superseded" })]);
    const region = await openSlottedCard();
    expect(visibleSlotTexts(region)).toEqual([
      "[“the key points”, reviewed in its own card: status not shown here]",
      "[“punchy”, reviewed in its own card: status not shown here]",
    ]);
    expect(Array.from(region.querySelectorAll(".ack-card-prompt-legend li")).map((item) => item.textContent)).toEqual([
      "Status not shown here — see its own card",
    ]);
  });

  it("gives each slot a screen-reader status prefix in the legend's words", async () => {
    setSlotEvents([vagueEvent("evt-topic", "the key points")], [vagueEvent("evt-tone", "punchy", { choice: "accepted_as_drafted" })]);
    const region = await openSlottedCard();
    expect(Array.from(region.querySelectorAll("mark .visually-hidden")).map((hidden) => hidden.textContent)).toEqual([
      "interpretation slot, pending: ",
      "interpretation slot, accepted: ",
    ]);
  });

  it("keys nothing for a prompt with no slots", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(inlineSnapshot({ prompt_template: "No slots here." }, ANCHOR_1));
    renderCard(makeEvent({ kind: "llm_prompt_template", llm_draft: "Bounded preview" }), { showAmend: false });
    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("No slots here."));
    expect(region.querySelector(".ack-card-prompt-legend")).toBeNull();
    expect(region.querySelector("mark")).toBeNull();
    expect(region).not.toHaveTextContent(/reviewed in its own card/);
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
