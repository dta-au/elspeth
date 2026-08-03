import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { CompletionBar } from "./CompletionBar";
import { ExportYamlModal } from "@/components/sidebar/ExportYamlModal";
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

// The Export-YAML dialog body renders YamlView, which pulls in session state
// and YAML rendering machinery irrelevant to the assertions in this file.
// Stub it so the dialog mounts deterministically and AC 7 can assert only the
// dialog itself (plan 19b:232).
vi.mock("@/components/inspector/YamlView", () => ({
  YamlView: () => (
    <button type="button" data-testid="yaml-view-stub">
      stub
    </button>
  ),
}));

function _validValidation(): ValidationResult {
  return makeValidationResult();
}

// Minimal composition with content: the Export-YAML button gates on
// pipeline content (elspeth-bff8043d33), NOT on validation state, so
// validation-axis tests must hold content constant.
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

  it("renders nothing without an active session", () => {
    const { container } = render(<CompletionBar />);
    expect(container.firstChild).toBeNull();
  });

  it("renders three co-equal buttons when a session is active and validation passes", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    expect(screen.getByTestId("completion-bar")).toBeInTheDocument();
    expect(screen.getByTestId("completion-bar-save-for-review")).toBeInTheDocument();
    expect(screen.getByTestId("completion-bar-run-pipeline")).toBeInTheDocument();
    expect(screen.getByTestId("completion-bar-export-yaml")).toBeInTheDocument();
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
      screen
        .getByTestId("completion-bar-run-pipeline")
        .querySelector("button"),
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
    const run = screen
      .getByTestId("completion-bar-run-pipeline")
      .querySelector("button") as HTMLButtonElement;

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

  // Plan 19b:229 — AC 4: Export-YAML button is enabled regardless of
  // VALIDATION state — it is the only completion-bar verb the design doc
  // (09) marks "Available always — even with warning status". Both extremes
  // are pinned with a content-bearing pipeline held constant: the button's
  // only gate is pipeline content (elspeth-bff8043d33), never validation.
  it("Export-YAML button ignores validation state on a non-empty pipeline (plan 19b:229, AC 4)", () => {
    // Case 1: no validation run yet (validationResult === null).
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionState: _nonEmptyComposition(),
    });
    // beforeEach already sets validationResult: null.
    const { unmount } = render(<CompletionBar />);
    const exportContainer = screen.getByTestId("completion-bar-export-yaml");
    const exportButtonNull = exportContainer.querySelector("button") as HTMLButtonElement;
    expect(exportButtonNull).not.toBeNull();
    expect(exportButtonNull.disabled).toBe(false);
    expect(exportButtonNull.getAttribute("aria-disabled")).toBeNull();
    unmount();

    // Case 2: explicit invalid validation — Save-for-review and Run-pipeline
    // both become disabled, but Export-YAML must remain enabled.
    useExecutionStore.setState({
      validationResult: _invalidValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const exportContainerInvalid = screen.getByTestId("completion-bar-export-yaml");
    const exportButtonInvalid = exportContainerInvalid.querySelector("button") as HTMLButtonElement;
    expect(exportButtonInvalid).not.toBeNull();
    expect(exportButtonInvalid.disabled).toBe(false);
    expect(exportButtonInvalid.getAttribute("aria-disabled")).toBeNull();
  });

  // elspeth-bff8043d33: on an EMPTY pipeline the three sibling completion
  // verbs must agree — Export-YAML is disabled with a stated reason instead
  // of opening a near-empty modal.
  it("Export-YAML button is disabled with a reason on an empty pipeline", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    // beforeEach leaves compositionState null (no pipeline yet).
    render(<CompletionBar />);
    const exportContainer = screen.getByTestId("completion-bar-export-yaml");
    const exportButton = exportContainer.querySelector("button") as HTMLButtonElement;
    expect(exportButton).not.toBeNull();
    expect(exportButton.disabled).toBe(true);
    expect(exportButton.getAttribute("aria-disabled")).toBe("true");
    expect(exportButton.getAttribute("title")).toMatch(/add pipeline components/i);
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
    const runContainer = screen.getByTestId("completion-bar-run-pipeline");
    const runButton = runContainer.querySelector("button") as HTMLButtonElement;
    expect(runButton).not.toBeNull();
    expect(runButton.disabled).toBe(false);

    fireEvent.click(runButton);

    // The disclosure gates execute(); confirming fires it.
    expect(executeSpy).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog", { name: /run pipeline\?/i });
    fireEvent.click(
      within(dialog).getByRole("button", { name: /^run pipeline$/i }),
    );

    expect(executeSpy).toHaveBeenCalledTimes(1);
    expect(executeSpy).toHaveBeenCalledWith("sess-RUN");
  });

  // Plan 19b:232 — AC 7: Clicking Export-YAML opens the existing YamlView
  // modal. ExportYamlButton dispatches a window CustomEvent
  // (OPEN_YAML_MODAL_EVENT) that ExportYamlModal listens for; rendering both
  // components together lets us assert the dialog becomes visible end-to-end.
  it("clicking Export-YAML opens the ExportYamlModal dialog (plan 19b:232, AC 7)", async () => {
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

    render(
      <>
        <CompletionBar />
        <ExportYamlModal />
      </>,
    );

    // Dialog should not be mounted before the click.
    expect(screen.queryByRole("dialog")).toBeNull();

    const exportContainer = screen.getByTestId("completion-bar-export-yaml");
    const exportButton = exportContainer.querySelector("button") as HTMLButtonElement;
    fireEvent.click(exportButton);

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: /export yaml/i })).toBeInTheDocument();
    });
  });
});
