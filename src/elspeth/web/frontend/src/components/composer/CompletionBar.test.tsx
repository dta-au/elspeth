import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { CompletionBar } from "./CompletionBar";
import { OPEN_IMPORT_YAML_MODAL_EVENT } from "@/lib/composer-events";
import { useShareableReviewStore } from "@/stores/shareableReviewStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import {
  COMPLETION_BLOCKED_VALIDATION_READINESS,
  INVALID_VALIDATION_READINESS,
  makeComposition,
  makeValidationResult,
} from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { ValidationResult } from "@/types/index";

function _validValidation(): ValidationResult {
  return makeValidationResult();
}

// Minimal composition with content, so the Import-YAML availability tests
// can pin both extremes of the content axis (empty is the beforeEach
// default).
function _nonEmptyComposition() {
  return makeComposition(1, {
    id: "state-1",
    sources: { source: { plugin: "csv", options: {} } },
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  });
}

function _invalidValidation(): ValidationResult {
  return makeValidationResult({
    is_valid: false,
    errors: [
      {
        component_id: "node1",
        component_type: "transform",
        message: "boom",
        suggestion: null,
      },
    ],
    readiness: INVALID_VALIDATION_READINESS,
  });
}

describe("CompletionBar", () => {
  beforeEach(() => {
    useSessionStore.setState({
      activeSessionId: null,
      compositionState: null,
    });
    useExecutionStore.setState({
      validationResult: null,
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    useShareableReviewStore.getState().reset();
    resetStore(useInterpretationEventsStore);
  });

  it("keeps the horizontal compact override scoped to the workspace action bar", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );

    expect(css).toMatch(
      /\.workspace-action-bar\s+\.completion-bar\s*\{[^}]*flex-direction:\s*row;[^}]*flex-wrap:\s*wrap;[^}]*padding:\s*0;/s,
    );
    expect(css).toMatch(
      /\.workspace-action-bar\s+\.completion-bar\s*>\s*\*\s*\{[^}]*flex:\s*1 1 [^;]+;[^}]*width:\s*auto;/s,
    );
    expect(css).not.toMatch(
      /(?:^|\n)\.completion-bar\s*\{[^}]*flex-direction:\s*row;/s,
    );
  });

  it("renders nothing without an active session", () => {
    const { container } = render(<CompletionBar />);
    expect(container.firstChild).toBeNull();
  });

  it("renders three co-equal buttons as DIRECT children of the group (elspeth-929fc5d4a7)", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const bar = screen.getByTestId("completion-bar");
    const buttons = within(bar).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Save for review",
      "Run pipeline",
      "Import YAML",
    ]);
    // Structural symmetry contract: a wrapper <div> around any verb makes
    // flexbox treat the three asymmetrically (the bare sibling stretches to
    // the tallest wrapper — the 98px slab). All three must be direct flex
    // children so the equal-width/height rules reach the buttons themselves.
    for (const button of buttons) {
      expect(button.parentElement).toBe(bar);
    }
  });

  it("disables Save for review when validation has not run", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    // validationResult: null — no validation has been run.
    render(<CompletionBar />);
    const button = screen.getByTestId("completion-bar-save-for-review") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("title")).toMatch(/fix validation/i);
  });

  it("disables Save for review when validation is invalid", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _invalidValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const button = screen.getByTestId("completion-bar-save-for-review") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("disables Save for review when a malformed validation response omits readiness", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      // Deliberately model untrusted wire data that violates the mandatory
      // TypeScript contract. Ordinary fixtures remain fully typed.
      validationResult: {
        is_valid: true,
        checks: [],
        errors: [],
        warnings: [],
      } as unknown as ValidationResult,
      isExecuting: false,
      progress: null,
    });

    render(<CompletionBar />);

    expect(screen.getByTestId("completion-bar-save-for-review")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Run pipeline" }),
    ).toBeDisabled();
  });

  it("enables Save for review when validation is valid", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const button = screen.getByTestId("completion-bar-save-for-review") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("blocks Save on completion readiness while leaving Run admitted", () => {
    const openAndMark = vi.fn();
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: {
        ..._validValidation(),
        readiness: COMPLETION_BLOCKED_VALIDATION_READINESS,
      },
      isExecuting: false,
      progress: null,
    });
    useShareableReviewStore.setState({ openAndMark });

    render(<CompletionBar />);
    const save = screen.getByTestId(
      "completion-bar-save-for-review",
    ) as HTMLButtonElement;
    const run = screen.getByRole("button", {
      name: "Run pipeline",
    }) as HTMLButtonElement;

    expect(save).toBeDisabled();
    expect(save).toHaveAttribute(
      "title",
      "Advisor sign-off is required before sharing for review.",
    );
    fireEvent.click(save);
    expect(openAndMark).not.toHaveBeenCalled();
    expect(run).not.toBeDisabled();
  });

  it("clicking Save for review invokes openAndMark with the active session id", () => {
    useSessionStore.setState({ activeSessionId: "sess-XYZ" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    const openAndMarkSpy = vi.fn();
    useShareableReviewStore.setState({ openAndMark: openAndMarkSpy });

    render(<CompletionBar />);
    fireEvent.click(screen.getByTestId("completion-bar-save-for-review"));
    expect(openAndMarkSpy).toHaveBeenCalledWith("sess-XYZ");
  });

  it("disables Save for review while a mark request is in flight", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    useShareableReviewStore.setState({ inFlight: true });
    render(<CompletionBar />);
    const button = screen.getByTestId("completion-bar-save-for-review") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  // Import-YAML is the completion-bar verb with NO gate beyond an active
  // session: importing into an EMPTY session is its primary use, and an
  // invalid pipeline may be replaced wholesale, so neither the content axis
  // nor the validation axis may disable it. Both extremes of each axis are
  // pinned here.
  it("Import-YAML button ignores validation and content state", () => {
    // Case 1: empty pipeline (beforeEach leaves compositionState null), no
    // validation run yet (validationResult === null).
    useSessionStore.setState({ activeSessionId: "sess-1" });
    const { unmount } = render(<CompletionBar />);
    const importButtonEmpty = screen.getByRole("button", {
      name: "Import YAML",
    }) as HTMLButtonElement;
    expect(importButtonEmpty.disabled).toBe(false);
    expect(importButtonEmpty.getAttribute("aria-disabled")).toBeNull();
    unmount();

    // Case 2: content-bearing pipeline with an explicit invalid validation —
    // Save-for-review and Run-pipeline both become disabled, but Import-YAML
    // must remain enabled.
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionState: _nonEmptyComposition(),
    });
    useExecutionStore.setState({
      validationResult: _invalidValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const importButtonInvalid = screen.getByRole("button", {
      name: "Import YAML",
    }) as HTMLButtonElement;
    expect(importButtonInvalid.disabled).toBe(false);
    expect(importButtonInvalid.getAttribute("aria-disabled")).toBeNull();
  });

  // Plan 19b:231 — AC 6: Clicking Run-pipeline calls the existing Execute
  // action from executionStore, via the pre-run egress disclosure
  // (elspeth-c18ad229cc): the ConfirmDialog gates execute(), so the click
  // opens it and confirming fires `execute(activeSessionId)`.
  it("clicking Run-pipeline calls executionStore.execute with the active session id after the egress disclosure (plan 19b:231, AC 6)", () => {
    const executeSpy = vi.fn();
    useSessionStore.setState({ activeSessionId: "sess-RUN" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: executeSpy,
      runDisclosureAckBySession: {},
    });

    render(<CompletionBar />);
    const runButton = screen.getByRole("button", {
      name: "Run pipeline",
    }) as HTMLButtonElement;
    expect(runButton.disabled).toBe(false);

    fireEvent.click(runButton);

    // The disclosure gates execute(); confirming fires it.
    expect(executeSpy).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog", { name: "Run pipeline" });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^run pipeline$/i }),
    );

    expect(executeSpy).toHaveBeenCalledTimes(1);
    expect(executeSpy).toHaveBeenCalledWith("sess-RUN");
  });

  it("clicking Import-YAML dispatches the open-import-modal event", () => {
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionState: _nonEmptyComposition(),
    });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    const opened = vi.fn();
    window.addEventListener(OPEN_IMPORT_YAML_MODAL_EVENT, opened);
    try {
      render(<CompletionBar />);
      fireEvent.click(screen.getByRole("button", { name: "Import YAML" }));
      // The modal itself is mounted at app-root level by ImportYamlModalHost;
      // this trigger's whole contract is the event.
      expect(opened).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(OPEN_IMPORT_YAML_MODAL_EVENT, opened);
    }
  });
});
