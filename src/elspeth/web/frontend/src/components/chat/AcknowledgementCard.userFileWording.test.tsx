// ============================================================================
// AcknowledgementCard — what the prompt card SAYS about where the instruction
// came from.
//
// The card's standing line is "The LLM wrote the instruction for this step."
// That is false when the whole reviewed prompt is files a user supplied. The
// wording follows the server's stored review snapshot, and only that: the
// prompt slots are all file bindings, and every binding is user-supplied,
// which means the PAIR `created_by == "user"` and a creation modality that is
// not LLM-authored. Any other loaded snapshot keeps the standing wording.
// Until the snapshot has loaded, and when it cannot be loaded, the card names
// no author at all. The wording is presentation only: the view gate and what
// Approve sends are pinned unchanged here.
// ============================================================================

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AcknowledgementCard } from "./AcknowledgementCard";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { InterpretationEvent, PromptReviewSnapshotResponse } from "@/types/interpretation";

vi.mock("@/api/client", () => ({
  listInterpretationEvents: vi.fn(),
  resolveInterpretation: vi.fn(),
  optOutOfInterpretations: vi.fn(),
  getInterpretationOptOutSummary: vi.fn(),
  getPromptReviewSnapshot: vi.fn(),
}));

import * as api from "@/api/client";

const LLM_LINE = "The LLM wrote the instruction for this step.";
const FILE_LINE = "The instruction for this step comes from a file supplied by a user.";
const NEUTRAL_LINE = "This step's instruction is waiting for your approval.";
const LLM_NOTE = "View prompt shows the LLM's instruction; Approve unlocks once you have viewed it.";
const FILE_NOTE = "View prompt shows the file's text; Approve unlocks once you have viewed it.";
const ANCHOR = "b".repeat(64);

type Binding = PromptReviewSnapshotResponse["bindings"][number];

const EVENT: InterpretationEvent = {
  id: "evt-1",
  session_id: "sess-1",
  composition_state_id: "state-1",
  affected_node_id: "node-1",
  tool_call_id: "tool-1",
  user_term: "llm_prompt_template:node-1",
  kind: "llm_prompt_template",
  llm_draft: "File-backed prompt pending review",
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
};

function marker(blobId: string): Record<string, string> {
  return { blob_ref: blobId, mode: "inline_content", sha256: "a".repeat(64) };
}

function binding(fieldPath: string, blobId: string, createdBy: unknown, modality: unknown): Binding {
  // `created_by` and `creation_modality` arrive as untyped JSON; the casts let
  // the tests send what a server could send, including nothing at all.
  return {
    field_path: fieldPath,
    blob_id: blobId,
    content_sha256: "a".repeat(64),
    encoding: "utf-8",
    byte_length: 11,
    filename: `${blobId}.txt`,
    creation_modality: modality as string,
    created_by: createdBy as string,
    provenance: {},
    text: `text of ${blobId}`,
  };
}

function snapshot(
  authoredSurface: Record<string, unknown>,
  bindings: Binding[],
  overrides: Partial<PromptReviewSnapshotResponse> = {},
): PromptReviewSnapshotResponse {
  return {
    version: 1,
    session_id: "sess-1",
    node_id: "node-1",
    event_id: EVENT.id,
    authored_surface: authoredSurface,
    bindings,
    anchor: ANCHOR,
    effective_artifact_hash: "c".repeat(64),
    is_live_review: true,
    ...overrides,
  };
}

function renderCard() {
  return render(<AcknowledgementCard event={EVENT} sessionId="sess-1" stepLabel="Summarise" showAmend={false} />);
}

/** Render, then wait until the snapshot request has settled into the card. */
async function renderSettled(): Promise<void> {
  renderCard();
  await waitFor(() => expect(api.getPromptReviewSnapshot).toHaveBeenCalled());
  await act(async () => {
    await Promise.resolve();
  });
}

function cardLine(): string {
  return document.querySelector(".ack-card-line")?.textContent ?? "";
}

function gateNote(): HTMLElement {
  const note = document.querySelector<HTMLElement>(".ack-card-gate-note");
  if (note === null) throw new Error("gate note not rendered");
  return note;
}

/** No author is named anywhere on the card: neither the LLM nor a user's file. */
function expectNoAttribution(): void {
  expect(cardLine()).toBe(NEUTRAL_LINE);
  expect(document.body).not.toHaveTextContent("The LLM wrote");
  expect(document.body).not.toHaveTextContent("LLM's instruction");
  expect(document.body).not.toHaveTextContent("supplied by a user");
  expect(document.body).not.toHaveTextContent("file's text");
}

function expectStandingWording(): void {
  expect(cardLine()).toBe(LLM_LINE);
  expect(gateNote()).toHaveTextContent(LLM_NOTE);
  expect(document.body).not.toHaveTextContent("supplied by a user");
}

beforeEach(() => {
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({ compositionState: null });
  vi.mocked(api.resolveInterpretation).mockReset();
  vi.mocked(api.getPromptReviewSnapshot).mockReset();
});

describe("prompt card wording — the prompt is entirely user-supplied files", () => {
  it.each([
    [
      "one user-supplied file",
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "verbatim")]),
    ],
    [
      "two user-supplied files",
      snapshot(
        { prompt_template: marker("u1"), system_prompt: marker("u2") },
        [binding("prompt_template", "u1", "user", "verbatim"), binding("system_prompt", "u2", "user", "verbatim")],
      ),
    ],
    [
      "user-supplied files on a mapping-form query and the system prompt",
      snapshot(
        { system_prompt: marker("u2"), queries: { q: { input_fields: {}, template: marker("u1") } } },
        [binding("queries.q.template", "u1", "user", "verbatim"), binding("system_prompt", "u2", "user", "verbatim")],
      ),
    ],
    [
      "a user-supplied file on a list-form query, system prompt not set",
      snapshot(
        { system_prompt: null, queries: [{ name: "q", input_fields: {}, template: marker("u1") }] },
        [binding("queries.0.template", "u1", "user", "verbatim")],
      ),
    ],
  ])("says the instruction comes from a user's file: %s", async (_name, reviewed) => {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(reviewed);
    await renderSettled();
    expect(cardLine()).toBe(FILE_LINE);
    expect(gateNote()).toHaveTextContent(FILE_NOTE);
    expect(document.body).not.toHaveTextContent("The LLM wrote");
    expect(document.body).not.toHaveTextContent("LLM's instruction");
  });

  it("keeps Approve disabled until the prompt has been viewed, and approves the same snapshot", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "verbatim")]),
    );
    vi.mocked(api.resolveInterpretation).mockReturnValue(new Promise(() => undefined));
    await renderSettled();
    expect(cardLine()).toBe(FILE_LINE);
    const approve = screen.getByRole("button", { name: /approve the llm prompt template/i });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    await user.click(approve);
    expect(api.resolveInterpretation).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "View prompt" }));
    const region = screen.getByRole("region", { name: /prompt template review/i });
    await waitFor(() => expect(region).toHaveTextContent("text of u1"));
    expect(approve).not.toHaveAttribute("aria-disabled");
    // Once viewed the gate note is gone; the line keeps the file wording.
    expect(cardLine()).toBe(FILE_LINE);

    await user.click(approve);
    await waitFor(() => expect(api.resolveInterpretation).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.resolveInterpretation).mock.calls[0]).toEqual([
      "sess-1",
      EVENT.id,
      { choice: "accepted_as_drafted", reviewed_anchor: ANCHOR },
    ]);
  });
});

describe("prompt card wording — every other case keeps the standing wording", () => {
  it.each([
    [
      "a file the assistant wrote (assistant, llm_generated)",
      snapshot({ prompt_template: marker("a1") }, [binding("prompt_template", "a1", "assistant", "llm_generated")]),
    ],
    [
      "a file the assistant created from the user's words (assistant, verbatim)",
      snapshot({ prompt_template: marker("a1") }, [binding("prompt_template", "a1", "assistant", "verbatim")]),
    ],
    [
      "a user-owned file whose content the LLM generated (user, llm_generated)",
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "llm_generated")]),
    ],
    [
      "a user-owned file the LLM generated and the user amended (user, llm_generated_then_amended)",
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "llm_generated_then_amended")]),
    ],
    [
      "a user-owned file the LLM disambiguated (user, disambiguated)",
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "disambiguated")]),
    ],
    [
      "one user-supplied file and one assistant-written file",
      snapshot(
        { prompt_template: marker("u1"), system_prompt: marker("a1") },
        [binding("prompt_template", "u1", "user", "verbatim"), binding("system_prompt", "a1", "assistant", "llm_generated")],
      ),
    ],
    [
      "a user-supplied file beside inline system text",
      snapshot({ prompt_template: marker("u1"), system_prompt: "You are helpful." }, [binding("prompt_template", "u1", "user", "verbatim")]),
    ],
    [
      "a user-supplied query file beside an inline query",
      snapshot(
        { queries: { q: { template: marker("u1") }, r: { template: "Inline {{ row['x'] }}" } } },
        [binding("queries.q.template", "u1", "user", "verbatim")],
      ),
    ],
    ["inline text only", snapshot({ prompt_template: "Summarise {{ row['text'] }}" }, [])],
    ["created_by missing", snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", undefined, "verbatim")])],
    ["created_by not a known author", snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "importer", "verbatim")])],
    ["creation modality missing", snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", undefined)])],
    ["creation modality not a known value", snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "imported")])],
    [
      "a prompt file with no binding in the snapshot",
      snapshot({ prompt_template: marker("u1"), system_prompt: marker("u2") }, [binding("prompt_template", "u1", "user", "verbatim")]),
    ],
  ])("keeps the LLM wording: %s", async (_name, reviewed) => {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(reviewed);
    await renderSettled();
    expectStandingWording();
  });

  it("keeps the LLM wording when the snapshot is for files that have since changed", async () => {
    vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(
      snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "verbatim")], { is_live_review: false }),
    );
    await renderSettled();
    expect(cardLine()).toBe(LLM_LINE);
    expect(document.body).not.toHaveTextContent("supplied by a user");
    expect(gateNote()).not.toHaveTextContent("file's text");
  });

  it("names no author when the snapshot cannot be loaded", async () => {
    vi.mocked(api.getPromptReviewSnapshot).mockRejectedValue({ status: 404, detail: "not found" });
    await renderSettled();
    expectNoAttribution();
    expect(screen.getByRole("button", { name: /approve the llm prompt template/i })).toHaveAttribute("aria-disabled", "true");
  });

  it.each([
    ["a user-supplied file", snapshot({ prompt_template: marker("u1") }, [binding("prompt_template", "u1", "user", "verbatim")]), FILE_LINE],
    ["a file the assistant wrote", snapshot({ prompt_template: marker("a1") }, [binding("prompt_template", "a1", "assistant", "llm_generated")]), LLM_LINE],
    ["inline text", snapshot({ prompt_template: "Summarise {{ row['text'] }}" }, []), LLM_LINE],
  ])("names no author while the snapshot is loading, then says which it is: %s", async (_name, reviewed, line) => {
    let deliver: (value: PromptReviewSnapshotResponse) => void = () => undefined;
    vi.mocked(api.getPromptReviewSnapshot).mockReturnValue(
      new Promise<PromptReviewSnapshotResponse>((resolve) => {
        deliver = resolve;
      }),
    );
    renderCard();
    await waitFor(() => expect(api.getPromptReviewSnapshot).toHaveBeenCalled());
    expectNoAttribution();
    expect(screen.getByRole("button", { name: /approve the llm prompt template/i })).toHaveAttribute("aria-disabled", "true");
    await act(async () => {
      deliver(reviewed);
      await Promise.resolve();
    });
    expect(cardLine()).toBe(line);
  });
});
