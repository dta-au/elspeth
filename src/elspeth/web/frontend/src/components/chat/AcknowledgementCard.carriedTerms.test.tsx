// ============================================================================
// AcknowledgementCard — what the prompt card shows at each term slot of a
// structured prompt, now that accepted terms carry across a wording edit.
//
// A carried term has no card of its own waiting: this prompt card is the
// only step before a run uses the term's definition at its new place. So:
//
//   - A slot whose frozen `event_id` is set shows THAT event's
//     `accepted_value` in place, whole, marked as approved earlier and as
//     "accepted as drafted" or "amended by you" (the amendment, never the
//     LLM's draft).
//   - A slot with `event_id: null` says the term is reviewed in its own card
//     and shows no definition. It never reads live state.
//   - Fail closed: an event that is not loaded, or does not match the slot
//     (wrong term, wrong kind, not resolved, no accepted value), or values
//     that do not reproduce the snapshot's rendered `prompt_template`, leaves
//     the card with no Approve and a stated reason.
//
// States (i)-(vi) are the carry builder's table (CARD-READ.md, "What the card
// can and cannot know"); F1/F2 are the red-team's sequences
// (reviews/carry-redteam.md).
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

const SESSION = "sess-1";
const NODE = "node-1";
const ANCHOR = "e".repeat(64);
const PENDING_TEXT = "pending interpretation";

const CATEGORY_DRAFT = "narrow categories";
const AUDIENCE_DRAFT = "LLM DRAFT for the audience";
const AUDIENCE_AMENDED = "new enterprise users";

const DEFINED_COPY =
  "Terms shown with a definition keep their approved definitions, shown in place; this card approves the wording around them.";
const MIXED_OWN_CARD_COPY = "A term marked “reviewed in its own card” has its value approved in that card.";
const OWN_CARD_ONLY_COPY =
  "A marked term is reviewed in its own card, which approves its value; this card approves the prompt around it.";

function baseEvent(overrides: Partial<InterpretationEvent>): InterpretationEvent {
  return {
    id: "evt-x",
    session_id: SESSION,
    composition_state_id: "state-1",
    affected_node_id: NODE,
    tool_call_id: "tool-1",
    user_term: null,
    kind: "vague_term",
    llm_draft: null,
    accepted_value: null,
    choice: "pending",
    created_at: "2026-11-19T00:00:00Z",
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

const PROMPT_EVENT = baseEvent({
  id: "evt-prompt",
  kind: "llm_prompt_template",
  user_term: "llm_prompt_template:node-1",
  llm_draft: "Bounded preview",
});

/** The term events as `GET /interpretations?status=all` returns them once resolved. */
const CATEGORY_EVENT = baseEvent({
  id: "evt-cat",
  user_term: "category",
  llm_draft: CATEGORY_DRAFT,
  accepted_value: CATEGORY_DRAFT,
  choice: "accepted_as_drafted",
  resolved_at: "2026-11-19T00:01:00Z",
});
const AUDIENCE_EVENT = baseEvent({
  id: "evt-aud",
  user_term: "audience",
  llm_draft: AUDIENCE_DRAFT,
  accepted_value: AUDIENCE_AMENDED,
  choice: "amended",
  resolved_at: "2026-11-19T00:02:00Z",
});

type Part = string | { ref: string };
interface Requirement {
  id: string;
  event_id: string | null;
  user_term: string | null;
}

const CAT_REQ: Requirement = { id: "req-cat", event_id: "evt-cat", user_term: "category" };
const AUD_REQ: Requirement = { id: "req-aud", event_id: "evt-aud", user_term: "audience" };

/** The run's value at each slot, as the server renders the snapshot's prompt_template. */
const RUN_VALUES: Record<string, string> = { "req-cat": CATEGORY_DRAFT, "req-aud": AUDIENCE_AMENDED };

/**
 * A structured snapshot surface. `prompt_template` is rendered the way the
 * server renders it: each slot naming an event gets that term's accepted
 * value, each slot with a null event gets "pending interpretation".
 */
function structuredSurface(
  parts: Part[],
  requirements: Requirement[],
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  const byId = new Map(requirements.map((requirement) => [requirement.id, requirement]));
  const rendered = parts
    .map((part) => {
      if (typeof part === "string") return part;
      return byId.get(part.ref)?.event_id === null ? PENDING_TEXT : RUN_VALUES[part.ref];
    })
    .join("");
  return {
    prompt_template: rendered,
    prompt_template_parts: parts.map((part) =>
      typeof part === "string"
        ? { kind: "text", text: part }
        : { kind: "interpretation_ref", requirement_id: part.ref },
    ),
    interpretation_requirements: requirements,
    ...extra,
  };
}

function snapshotOf(surface: Record<string, unknown>): PromptReviewSnapshotResponse {
  return {
    version: 1,
    session_id: SESSION,
    node_id: NODE,
    event_id: PROMPT_EVENT.id,
    authored_surface: surface,
    bindings: [],
    anchor: ANCHOR,
    effective_artifact_hash: null,
    is_live_review: true,
  };
}

/** Two terms in one sentence: the red-team's fixture prompt. */
const TWO_TERMS: Part[] = ["Sort {{ row.text }} into ", { ref: "req-cat" }, " for ", { ref: "req-aud" }, "."];

function setEvents(pending: InterpretationEvent[], resolved: InterpretationEvent[]) {
  useInterpretationEventsStore.setState({
    pendingBySession: { [SESSION]: Object.fromEntries(pending.map((event) => [event.id, event])) },
    resolvedBySession: { [SESSION]: resolved },
  });
}

async function openCard(surface: Record<string, unknown>): Promise<HTMLElement> {
  const user = userEvent.setup();
  vi.mocked(api.getPromptReviewSnapshot).mockResolvedValue(snapshotOf(surface));
  render(<AcknowledgementCard event={PROMPT_EVENT} sessionId={SESSION} stepLabel="Classify" showAmend={false} />);
  await user.click(screen.getByRole("button", { name: "View prompt" }));
  const region = screen.getByRole("region", { name: /prompt template review/i });
  await waitFor(() => expect(region.querySelectorAll("mark.ack-card-prompt-slot").length).toBeGreaterThan(0));
  return region;
}

/** The visible text of each slot mark (the sr-only prefix removed). */
function slotTexts(region: HTMLElement): string[] {
  return Array.from(region.querySelectorAll("mark.ack-card-prompt-slot")).map((mark) => {
    const clone = mark.cloneNode(true) as HTMLElement;
    clone.querySelectorAll(".visually-hidden").forEach((hidden) => hidden.remove());
    return clone.textContent ?? "";
  });
}

function srPrefixes(region: HTMLElement): string[] {
  return Array.from(region.querySelectorAll("mark .visually-hidden")).map((hidden) => hidden.textContent ?? "");
}

function notes(region: HTMLElement): string[] {
  return Array.from(region.querySelectorAll(".ack-card-prompt-pending-note")).map((note) => note.textContent ?? "");
}

function legend(region: HTMLElement): string[] {
  return Array.from(region.querySelectorAll(".ack-card-prompt-legend li")).map((item) => item.textContent ?? "");
}

function approveButton(): HTMLElement {
  return screen.getByRole("button", { name: /approve the llm prompt template/i });
}

const drafted = (term: string, value: string) => `[“${term}”, approved earlier, accepted as drafted: ${value}]`;
const amended = (term: string, value: string) => `[“${term}”, approved earlier, amended by you: ${value}]`;
const ownCard = (term: string, status: string) => `[“${term}”, reviewed in its own card: ${status}]`;
const notShown = (term: string) => `[“${term}”, approved definition cannot be shown]`;

/** Approve is offered and sends the snapshot's anchor. */
async function expectApprovable(): Promise<void> {
  const user = userEvent.setup();
  vi.mocked(api.resolveInterpretation).mockResolvedValue({
    event: { ...PROMPT_EVENT, choice: "accepted_as_drafted", accepted_value: "x", resolved_at: "2026-11-19T00:03:00Z" },
    new_state: null,
  } as unknown as Awaited<ReturnType<typeof api.resolveInterpretation>>);
  const approve = approveButton();
  expect(approve).not.toHaveAttribute("aria-disabled");
  await user.click(approve);
  await waitFor(() => expect(api.resolveInterpretation).toHaveBeenCalledTimes(1));
  expect(api.resolveInterpretation).toHaveBeenCalledWith(SESSION, PROMPT_EVENT.id, {
    choice: "accepted_as_drafted",
    reviewed_anchor: ANCHOR,
  });
}

/** No Approve: gated with the stated reason, and a click sends nothing. */
async function expectFailClosed(reason: string): Promise<void> {
  const user = userEvent.setup();
  const approve = approveButton();
  expect(approve).toHaveAttribute("aria-disabled", "true");
  const note = document.querySelector<HTMLElement>(".ack-card-gate-note");
  expect(note?.textContent).toBe(reason);
  expect(approve.getAttribute("aria-describedby")).toBe(note?.id);
  await user.click(approve);
  expect(api.resolveInterpretation).not.toHaveBeenCalled();
}

const notLoaded = (term: string) =>
  `The approved definition of “${term}” is not loaded, so this prompt cannot be approved. Reload the session to load it.`;
const mismatched = (term: string) =>
  `The approved definition of “${term}” does not match this prompt review, so it cannot be approved. Reload the session to see the current review.`;
const RENDER_MISMATCH =
  "The approved definitions do not reproduce the reviewed prompt, so it cannot be approved. Reload the session to see the current review.";

beforeEach(() => {
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({ compositionState: null });
  vi.mocked(api.resolveInterpretation).mockReset();
  vi.mocked(api.getPromptReviewSnapshot).mockReset();
});

describe("prompt card — each slot by state (i)-(vi)", () => {
  it("(i) a term pending when the card was made: reviewed in its own card, pending, no definition", async () => {
    setEvents([baseEvent({ id: "evt-cat-open", user_term: "category", llm_draft: CATEGORY_DRAFT })], []);
    const region = await openCard(
      structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, { ...AUD_REQ, event_id: null }]),
    );
    expect(slotTexts(region)).toEqual([ownCard("category", "pending"), ownCard("audience", "status not shown here")]);
    expect(region).not.toHaveTextContent(CATEGORY_DRAFT);
    expect(notes(region)).toEqual([OWN_CARD_ONLY_COPY]);
    await expectApprovable();
  });

  it("(ii) a null slot whose term resolved later: accepted, and the live value is never shown", async () => {
    setEvents([], [{ ...CATEGORY_EVENT, accepted_value: "LIVE VALUE" }]);
    const region = await openCard(structuredSurface(["Sort into ", { ref: "req-cat" }, "."], [{ ...CAT_REQ, event_id: null }]));
    expect(slotTexts(region)).toEqual([ownCard("category", "accepted")]);
    expect(region).not.toHaveTextContent("LIVE VALUE");
    await expectApprovable();
  });

  it("(iii) a carried term accepted as drafted: its accepted value in place, marked approved earlier", async () => {
    setEvents([], [CATEGORY_EVENT]);
    const region = await openCard(structuredSurface(["Sort into ", { ref: "req-cat" }, "."], [CAT_REQ]));
    expect(slotTexts(region)).toEqual([drafted("category", CATEGORY_DRAFT)]);
    expect(srPrefixes(region)).toEqual(["interpretation slot, accepted: "]);
    expect(region.querySelector("mark.ack-card-prompt-slot--accepted")).not.toBeNull();
    expect(notes(region)).toEqual([DEFINED_COPY]);
    await expectApprovable();
  });

  it("(iv) a carried term the user amended: the amendment in place, never the LLM's draft", async () => {
    setEvents([], [AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]));
    expect(slotTexts(region)).toEqual([amended("audience", AUDIENCE_AMENDED)]);
    expect(region).not.toHaveTextContent(AUDIENCE_DRAFT);
    await expectApprovable();
  });

  it("(v) a duplicated slot: one event fills every place the term appears", async () => {
    setEvents([], [AUDIENCE_EVENT]);
    const region = await openCard(
      structuredSurface(["For ", { ref: "req-aud" }, ", and again for ", { ref: "req-aud" }, "."], [AUD_REQ]),
    );
    expect(slotTexts(region)).toEqual([amended("audience", AUDIENCE_AMENDED), amended("audience", AUDIENCE_AMENDED)]);
    await expectApprovable();
  });

  it("(vi) a re-drafted term beside a carried one: the carried term shows its definition, the other its own card", async () => {
    const redrafted = baseEvent({ id: "evt-cat-2", user_term: "category", llm_draft: "narrow categories v2" });
    // The OLD category review is still in the list; the null slot must not take its value.
    setEvents([redrafted], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, AUD_REQ]));
    expect(slotTexts(region)).toEqual([ownCard("category", "status not shown here"), amended("audience", AUDIENCE_AMENDED)]);
    expect(region).not.toHaveTextContent(CATEGORY_DRAFT);
    expect(region).not.toHaveTextContent("narrow categories v2");
    expect(notes(region)).toEqual([DEFINED_COPY, MIXED_OWN_CARD_COPY]);
    await expectApprovable();
  });

  it("(vi, pending) the re-drafted term's own card open alone: pending beside the carried definition", async () => {
    const redrafted = baseEvent({ id: "evt-cat-2", user_term: "category", llm_draft: "narrow categories v2" });
    setEvents([redrafted], [AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, AUD_REQ]));
    expect(slotTexts(region)).toEqual([ownCard("category", "pending"), amended("audience", AUDIENCE_AMENDED)]);
    expect(srPrefixes(region)).toEqual(["interpretation slot, pending: ", "interpretation slot, accepted: "]);
  });

  it("two terms in one sentence, both carried", async () => {
    setEvents([], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(TWO_TERMS, [CAT_REQ, AUD_REQ]));
    expect(slotTexts(region)).toEqual([drafted("category", CATEGORY_DRAFT), amended("audience", AUDIENCE_AMENDED)]);
    expect(region.querySelector("pre.ack-card-prompt-pre")?.textContent).toBe(
      `Sort {{ row.text }} into interpretation slot, accepted: ${drafted("category", CATEGORY_DRAFT)} for ` +
        `interpretation slot, accepted: ${amended("audience", AUDIENCE_AMENDED)}.`,
    );
    await expectApprovable();
  });

  it("shows a long amended definition whole", async () => {
    const long = `${"long amended definition ".repeat(340)}END-OF-DEFINITION`;
    expect(new TextEncoder().encode(long).length).toBeGreaterThan(8000);
    RUN_VALUES["req-aud"] = long;
    try {
      setEvents([], [{ ...AUDIENCE_EVENT, accepted_value: long }]);
      const region = await openCard(structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]));
      expect(slotTexts(region)).toEqual([amended("audience", long)]);
      await expectApprovable();
    } finally {
      RUN_VALUES["req-aud"] = AUDIENCE_AMENDED;
    }
  });

  it("follows the slot's event id, not the term: an earlier review of the same term is not shown", async () => {
    const older = { ...AUDIENCE_EVENT, id: "evt-aud-old", accepted_value: "OLDER VALUE", choice: "accepted_as_drafted" as const };
    setEvents([], [older, AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]));
    expect(slotTexts(region)).toEqual([amended("audience", AUDIENCE_AMENDED)]);
    expect(region).not.toHaveTextContent("OLDER VALUE");
  });
});

describe("prompt card — fail closed on a slot naming an event", () => {
  const ONE_SLOT = structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]);

  it("the event is not loaded", async () => {
    setEvents([], []);
    const region = await openCard(ONE_SLOT);
    expect(slotTexts(region)).toEqual([notShown("audience")]);
    expect(srPrefixes(region)).toEqual(["interpretation slot, status not shown here: "]);
    // No sentence claims the term is reviewed in its own card: the gate note says why.
    expect(notes(region)).toEqual([]);
    await expectFailClosed(notLoaded("audience"));
  });

  it("beside a term reviewed in its own card, the copy speaks only of that term", async () => {
    setEvents([baseEvent({ id: "evt-cat-open", user_term: "category" })], []);
    const region = await openCard(structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, AUD_REQ]));
    expect(slotTexts(region)).toEqual([ownCard("category", "pending"), notShown("audience")]);
    expect(notes(region)).toEqual([MIXED_OWN_CARD_COPY]);
    await expectFailClosed(notLoaded("audience"));
  });

  it("becomes approvable once the event list loads, after the prompt is viewed", async () => {
    setEvents([], []);
    const region = await openCard(ONE_SLOT);
    await expectFailClosed(notLoaded("audience"));
    act(() => setEvents([], [AUDIENCE_EVENT]));
    expect(slotTexts(region)).toEqual([amended("audience", AUDIENCE_AMENDED)]);
    await expectApprovable();
  });

  it.each([
    ["names another term", { user_term: "category" }],
    ["is not a vague term", { kind: "llm_model_choice" as const }],
    ["is still pending", { choice: "pending" as const, accepted_value: null, resolved_at: null }],
    ["was superseded", { choice: "superseded" as const }],
    ["has no accepted value", { accepted_value: null }],
  ])("the event %s", async (_case, overrides) => {
    const wrong = { ...AUDIENCE_EVENT, ...overrides };
    setEvents(wrong.choice === "pending" ? [wrong] : [], wrong.choice === "pending" ? [] : [wrong]);
    const region = await openCard(ONE_SLOT);
    expect(slotTexts(region)).toEqual([notShown("audience")]);
    expect(region).not.toHaveTextContent(AUDIENCE_AMENDED);
    await expectFailClosed(mismatched("audience"));
  });

  it("the values do not reproduce the snapshot's rendered prompt", async () => {
    setEvents([], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const surface = { ...structuredSurface(TWO_TERMS, [CAT_REQ, AUD_REQ]), prompt_template: "Sort {{ row.text }} into narrow categories for SOMEONE ELSE." };
    const region = await openCard(surface);
    expect(slotTexts(region)).toEqual([notShown("category"), notShown("audience")]);
    expect(region).not.toHaveTextContent(AUDIENCE_AMENDED);
    await expectFailClosed(RENDER_MISMATCH);
  });

  it("the snapshot carries no rendered prompt to check against", async () => {
    setEvents([], [AUDIENCE_EVENT]);
    const { prompt_template: _dropped, ...surface } = ONE_SLOT;
    const region = await openCard(surface);
    expect(slotTexts(region)).toEqual([notShown("audience")]);
    await expectFailClosed(RENDER_MISMATCH);
  });
});

describe("prompt card — the legend for a definition that cannot be shown", () => {
  const NOT_SHOWN_KEY = "Definition not shown: see the note above";
  const UNKNOWN_KEY = "Status not shown here — see its own card";

  it("keys a not-shown slot as such, not as a term with its own card", async () => {
    setEvents([], []);
    const region = await openCard(structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]));
    expect(legend(region)).toEqual([NOT_SHOWN_KEY]);
  });

  it("keeps the unknown-status key only for a null-event slot whose status is unknown", async () => {
    setEvents([], []);
    const region = await openCard(structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, AUD_REQ]));
    expect(slotTexts(region)).toEqual([ownCard("category", "status not shown here"), notShown("audience")]);
    expect(legend(region)).toEqual([UNKNOWN_KEY, NOT_SHOWN_KEY]);
  });

  it("beside a pending own-card term, keys pending and not shown", async () => {
    setEvents([baseEvent({ id: "evt-cat-open", user_term: "category" })], []);
    const region = await openCard(structuredSurface(TWO_TERMS, [{ ...CAT_REQ, event_id: null }, AUD_REQ]));
    expect(legend(region)).toEqual(["Pending — awaiting its own review", NOT_SHOWN_KEY]);
  });

  it("a shown definition is keyed as accepted", async () => {
    setEvents([], [AUDIENCE_EVENT]);
    const region = await openCard(structuredSurface(["Write for ", { ref: "req-aud" }, "."], [AUD_REQ]));
    expect(legend(region)).toEqual(["Accepted — its own review is approved"]);
  });
});

describe("prompt card — the red-team's sequences", () => {
  it("F1 sequence A: a long prompt with a system prompt, whose bounded draft cuts both values off", async () => {
    // Step 1 padded the first text part (rendered prompt ~5080 chars); step 2
    // added a system prompt. The card's llm_draft is cut and holds neither value.
    const padded = `Sort {{ row.text }} ${"with careful attention to detail ".repeat(150)}into `;
    const surface = structuredSurface([padded, { ref: "req-cat" }, " for ", { ref: "req-aud" }, "."], [CAT_REQ, AUD_REQ], {
      system_prompt: "You classify rows.",
    });
    expect((surface.prompt_template as string).length).toBeGreaterThan(5000);
    setEvents([], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const region = await openCard(surface);
    expect(PROMPT_EVENT.llm_draft).not.toContain(AUDIENCE_AMENDED);
    expect(region).toHaveTextContent("You classify rows.");
    expect(slotTexts(region)).toEqual([drafted("category", CATEGORY_DRAFT), amended("audience", AUDIENCE_AMENDED)]);
    // The definitions sit in the user prompt, after the padded text.
    const userPrompt = region.querySelectorAll("pre.ack-card-prompt-pre")[1];
    expect(userPrompt.textContent).toContain(`into interpretation slot, accepted: ${drafted("category", CATEGORY_DRAFT)}`);
    await expectApprovable();
  });

  it("F1 sequence B: a multi-query node with sixty long-named queries", async () => {
    const queries = Array.from({ length: 60 }, (_, index) => ({
      name: `classification_query_with_a_rather_long_descriptive_name_${index}`,
    }));
    const surface = structuredSurface(TWO_TERMS, [CAT_REQ, AUD_REQ], { queries });
    setEvents([], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const region = await openCard(surface);
    expect(region).toHaveTextContent("Query 'classification_query_with_a_rather_long_descriptive_name_59': uses the node-level prompt_template");
    expect(slotTexts(region)).toEqual([drafted("category", CATEGORY_DRAFT), amended("audience", AUDIENCE_AMENDED)]);
    await expectApprovable();
  });

  it("F2 p6_moved_snapshot: a carried term moved, duplicated and put under a negation shows its definition at every place", async () => {
    // Requirements are listed once each, in first-use order.
    const surface = structuredSurface(
      ["Never put {{ row.text }} for ", { ref: "req-aud" }, "; ignore ", { ref: "req-cat" }, " and also ", { ref: "req-aud" }, "."],
      [AUD_REQ, CAT_REQ],
    );
    setEvents([], [CATEGORY_EVENT, AUDIENCE_EVENT]);
    const region = await openCard(surface);
    expect(slotTexts(region)).toEqual([
      amended("audience", AUDIENCE_AMENDED),
      drafted("category", CATEGORY_DRAFT),
      amended("audience", AUDIENCE_AMENDED),
    ]);
    const pieces = Array.from(region.querySelectorAll("pre.ack-card-prompt-pre > span")).map((span) => span.textContent);
    expect(pieces).toEqual(["Never put {{ row.text }} for ", "; ignore ", " and also ", "."]);
    await expectApprovable();
  });
});
