/**
 * Copy-register guard for the two surfaces that NAME the product's commands:
 * the command palette (where users discover a command) and the keyboard
 * shortcuts sheet (where they look it up again seconds later).
 *
 * Two constraints, both derived rather than transcribed — these read the
 * labels out of the rendered DOM, so they fail if either component's literals
 * drift, and they do not pin any single string as such:
 *
 *   1. REGISTER — every product-authored command label is sentence case.
 *      (elspeth-3db2ae2f48: "Show Graph" was the lone Title Case row in the
 *      shortcuts sheet; elspeth-93897c03d1: six palette titles were Title
 *      Case.)
 *   2. AGREEMENT — a chord documented on both surfaces carries the SAME label
 *      on both. (elspeth-93897c03d1: the same Ctrl+N was "New Session" in the
 *      palette and "New session" in the sheet.)
 *
 * Session commands are excluded from (1): their title is user data, not
 * product copy.
 */
import { render, screen, within, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPalette } from "./CommandPalette";
import { ShortcutsHelp } from "./ShortcutsHelp";
import { useSessionStore } from "@/stores/sessionStore";
import {
  makeComposition,
  makeValidationResult,
  READY_VALIDATION_READINESS,
} from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { GuidedSession } from "@/types/guided";
import type { Session, ValidationResult } from "@/types/index";

function makeSession(id: string, title: string): Session {
  return {
    id,
    title,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  };
}

vi.mock("@/api/client", () => ({
  fetchSessions: vi.fn(),
  createSession: vi.fn(),
  fetchMessages: vi.fn(),
  fetchCompositionState: vi.fn(),
  fetchComposerProgress: vi.fn(),
  sendMessage: vi.fn(),
  recompose: vi.fn(),
  forkFromMessage: vi.fn(),
  revertToVersion: vi.fn(),
  fetchStateVersions: vi.fn(),
  archiveSession: vi.fn(),
  getGuided: vi.fn(),
  respondGuided: vi.fn(),
  reenterGuided: vi.fn(),
  chatGuided: vi.fn(),
  fetchYaml: vi.fn(),
}));

const executionStoreState = vi.hoisted(() => ({
  execute: vi.fn(),
  validationResult: null as ValidationResult | null,
  isExecuting: false,
  progress: null as null | { status: string },
}));

vi.mock("@/stores/executionStore", () => ({
  useExecutionStore: (selector: (state: unknown) => unknown) =>
    selector(executionStoreState),
}));

const exitedGuidedSession: GuidedSession = {
  step: "step_1_source",
  history: [],
  terminal: {
    kind: "exited_to_freeform",
    reason: "user_pressed_exit",
    pipeline_yaml: null,
  },
  chat_history: [],
  chat_turn_seq: 0,
  profile: null,
};

/**
 * The shortcuts sheet spells the platform-agnostic chord "Ctrl/Cmd+…" where
 * the palette prints the literal key event it listens for ("Ctrl+…"). Same
 * chord, two renderings; fold them together before comparing labels.
 */
function normalizeChord(chord: string): string {
  return chord.replace("Ctrl/Cmd", "Ctrl");
}

/**
 * Sentence case, as this product means it: the first word is capitalised and
 * every later word is lower case, unless it is an acronym (all upper case,
 * e.g. YAML). Parenthesised segments are dropped first — they enumerate the
 * proper names of product surfaces ("(Sources / Transforms / Sinks)"), which
 * keep their capitals.
 */
function offendsSentenceCase(label: string): string[] {
  const prose = label.replace(/\([^)]*\)/g, " ").trim();
  const words = prose.split(/\s+/).filter((word) => word.length > 0);
  const offenders: string[] = [];
  if (words.length > 0 && words[0] !== "" && words[0][0] !== words[0][0].toUpperCase()) {
    offenders.push(words[0]);
  }
  for (const word of words.slice(1)) {
    const letters = word.replace(/[^A-Za-z]/g, "");
    if (letters === "") continue;
    const isAcronym = letters === letters.toUpperCase();
    const isLowerCase = letters === letters.toLowerCase();
    if (!isAcronym && !isLowerCase) offenders.push(word);
  }
  return offenders;
}

/** (chord → label) for every row of the rendered shortcuts sheet. */
function readShortcutsSheet(): Map<string, string> {
  const rows = new Map<string, string>();
  for (const item of document.querySelectorAll(".shortcuts-list-item")) {
    const chord = item.querySelector("kbd")?.textContent?.trim() ?? "";
    const label = item.querySelector("dd")?.textContent?.trim() ?? "";
    expect(chord).not.toBe("");
    expect(label).not.toBe("");
    rows.set(normalizeChord(chord), label);
  }
  return rows;
}

/**
 * (label → chord|null) for every palette command the user can act on, minus
 * the Sessions group whose titles are user data rather than product copy.
 */
function readPaletteCommands(): { label: string; chord: string | null }[] {
  const commands: { label: string; chord: string | null }[] = [];
  const sessionGroup = document
    .querySelector("#cmd-group-session")
    ?.closest(".command-palette-group");
  for (const option of document.querySelectorAll<HTMLElement>(
    ".command-palette-item",
  )) {
    if (sessionGroup !== null && sessionGroup !== undefined && sessionGroup.contains(option)) {
      continue;
    }
    const label =
      option
        .querySelector(".command-palette-item-title")
        ?.textContent?.trim() ?? "";
    expect(label).not.toBe("");
    const chord = option.querySelector("kbd")?.textContent?.trim() ?? null;
    commands.push({ label, chord: chord === null ? null : normalizeChord(chord) });
  }
  return commands;
}

/** Every command the palette can offer, so the guard sees the whole corpus. */
function renderFullyStockedPalette() {
  useSessionStore.setState({
    activeSessionId: "session-1",
    sessions: [
      makeSession("session-1", "Session 1"),
      makeSession("session-2", "Some User Session Title"),
    ],
    compositionState: makeComposition(1),
    guidedSession: exitedGuidedSession,
  });
  executionStoreState.validationResult = makeValidationResult({
    readiness: READY_VALIDATION_READINESS,
  });
  render(<CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />);
}

describe("command copy register", () => {
  beforeEach(() => {
    executionStoreState.validationResult = null;
    executionStoreState.isExecuting = false;
    executionStoreState.progress = null;
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps every shortcuts-sheet action label in sentence case", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const sheet = readShortcutsSheet();
    expect(sheet.size).toBeGreaterThan(5);

    const offences = [...sheet.values()]
      .map((label) => ({ label, offenders: offendsSentenceCase(label) }))
      .filter((entry) => entry.offenders.length > 0);
    expect(offences).toEqual([]);
  });

  it("keeps the shortcuts dialog title in sentence case", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const title = screen.getByRole("heading", { level: 2 }).textContent ?? "";
    expect(offendsSentenceCase(title)).toEqual([]);
  });

  it("keeps every shortcuts group heading in sentence case", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent ?? "");
    expect(headings.length).toBeGreaterThan(0);
    for (const heading of headings) {
      expect(offendsSentenceCase(heading)).toEqual([]);
    }
  });

  it("keeps every product-authored palette command title in sentence case", () => {
    renderFullyStockedPalette();
    const commands = readPaletteCommands();
    // Guard the guard: if the store wiring above ever stops stocking the
    // palette, an empty corpus would pass vacuously.
    expect(commands.length).toBeGreaterThan(5);

    const offences = commands
      .map(({ label }) => ({ label, offenders: offendsSentenceCase(label) }))
      .filter((entry) => entry.offenders.length > 0);
    expect(offences).toEqual([]);
  });

  it("does not hold user-supplied session titles to the product register", () => {
    renderFullyStockedPalette();
    const sessionGroup = document
      .querySelector("#cmd-group-session")
      ?.closest(".command-palette-group") as HTMLElement | null;
    expect(sessionGroup).not.toBeNull();
    // "Some User Session Title" is Title Case on purpose: it is user data,
    // and it must be excluded from the corpus the register guard inspects.
    expect(
      within(sessionGroup as HTMLElement).getByText("Some User Session Title"),
    ).toBeInTheDocument();
    expect(readPaletteCommands().map((c) => c.label)).not.toContain(
      "Some User Session Title",
    );
  });

  it("names a chord the same way in the palette and in the shortcuts sheet, wherever both already agree on the words", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const sheet = readShortcutsSheet();
    cleanup();
    renderFullyStockedPalette();
    const commands = readPaletteCommands();

    const shared = commands
      .filter((command) => command.chord !== null && sheet.has(command.chord))
      .map((command) => ({
        chord: command.chord as string,
        palette: command.label,
        sheet: sheet.get(command.chord as string) as string,
      }));
    expect(shared.length).toBeGreaterThan(3);

    // Two shared chords name the same action with DIFFERENT WORDS, not
    // different case: Ctrl+E is "Execute pipeline" in the palette and "Run
    // pipeline" in the sheet, and Ctrl+Shift+Y is "Export YAML" against "Show
    // YAML". Settling those is a copy decision that also moves Playwright
    // selectors under tests/e2e/, so they are enumerated rather than silently
    // tolerated. The assertion is SUBSET, not equality: no NEW split may open,
    // but closing one of these two must not redden the test.
    const knownWordChoiceSplits = ["Ctrl+E", "Ctrl+Shift+Y"];
    for (const entry of shared) {
      if (knownWordChoiceSplits.includes(entry.chord)) continue;
      expect(
        `${entry.chord}: ${entry.palette}`,
        `palette and shortcuts sheet disagree on ${entry.chord}`,
      ).toBe(`${entry.chord}: ${entry.sheet}`);
    }
  });
});
