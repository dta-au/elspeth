import { readFileSync } from "node:fs";
import { join } from "node:path";

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAuditReadinessStore } from "@/stores/auditReadinessStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import {
  makeComposition,
  makeValidationResult,
} from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { AuditReadinessSnapshot } from "@/types/api";
import { WorkspaceActionBar } from "./WorkspaceActionBar";
import { WorkspacePaneProvider } from "./WorkspacePaneContext";
import type { WorkspacePaneState } from "./useWorkspacePaneState";

vi.mock("@/components/composer/CompletionBar", () => ({
  CompletionBar: () => <div data-testid="completion-bar">Completion actions</div>,
}));

function renderActionBar(
  capabilities: { completion: boolean },
) {
  const openInspector = vi.fn();
  const paneState: WorkspacePaneState = {
    paneBounds: { min: 360, max: 640, defaultWidth: 420, resizable: true },
    preferredAuthoringWidth: 420,
    effectiveAuthoringWidth: 420,
    authoringCollapsed: false,
    availableArtifactTabs: ["graph", "spec", "yaml", "run"],
    activeArtifactTab: "graph",
    activeInspectorTab: null,
    inspectorOpen: false,
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
    openInspector,
    closeInspector: vi.fn(),
  };
  const view = render(
    <WorkspacePaneProvider paneState={paneState}>
      <WorkspaceActionBar capabilities={capabilities} />
    </WorkspacePaneProvider>,
  );
  return { ...view, openInspector };
}

function freshAuditSnapshot(): AuditReadinessSnapshot {
  return {
    session_id: "session-1",
    composition_version: 4,
    checked_at: "2026-08-11T00:00:00Z",
    rows: [
      "validation",
      "plugin_trust",
      "provenance",
      "retention",
      "llm_interpretations",
      "secrets",
    ].map((id) => ({
      id,
      label: id,
      status: "ok",
      summary: "Ready",
      detail: null,
      component_ids: [],
    })) as AuditReadinessSnapshot["rows"],
    validation_result: makeValidationResult(),
  };
}

describe("WorkspaceActionBar", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    useExecutionStore.getState().reset();
    useAuditReadinessStore.getState().reset();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: makeComposition(4),
    } as never);
  });

  it("keeps every control in the action bar on one height register", () => {
    // elspeth-5413e4221e: the strip used to mix two registers — 44px .btn
    // completion buttons beside --size-control-compact status chips and
    // "More actions" — leaving a 4px step at BOTH edges of one 8px-padded
    // row. The chips were promoted rather than the primaries demoted, because
    // .workspace-collapse-control across the pane divider is pinned to
    // --size-control expressly to register against this row; demoting would
    // have put the primaries out of register with it.
    //
    // Asserted as a SET rather than per-selector so a future edit cannot
    // silently reintroduce a second register in this strip: every min-height
    // declared by an action-bar rule must resolve to the same token.
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");

    const barRegisters = new Map<string, string>();
    for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = rule[1].trim();
      if (
        !/^\.workspace-(status-control|collapse-control)\b/.test(
          selector,
        )
      ) {
        continue;
      }
      const minHeight = /min-height:\s*([^;]+);/.exec(rule[2]);
      if (minHeight !== null) {
        barRegisters.set(selector, minHeight[1].trim());
      }
    }

    expect([...barRegisters.keys()].sort()).toEqual([
      ".workspace-collapse-control",
      ".workspace-status-control",
    ]);
    expect([...new Set(barRegisters.values())]).toEqual([
      "var(--size-control)",
    ]);
  });

  it("always renders text-labelled validation and audit inspector controls", async () => {
    const user = userEvent.setup();
    useExecutionStore.setState({ validationResult: makeValidationResult() });
    useAuditReadinessStore.setState({
      snapshotsBySession: { "session-1": freshAuditSnapshot() },
    });
    const { openInspector, container } = renderActionBar({
      completion: false,
    });

    const actionBar = container.querySelector(".workspace-action-bar");
    expect(actionBar).not.toBeNull();
    const statusGroup = screen.getByRole("group", { name: "Workspace status" });
    expect(statusGroup).toHaveAttribute("data-workspace-status-controls", "true");
    const validation = screen.getByRole("button", {
      name: "Validation: Passed",
    });
    const audit = screen.getByRole("button", { name: "Audit: Ready" });
    expect(validation).toHaveTextContent("Validation:Passed");
    expect(audit).toHaveTextContent("Audit:Ready");

    await user.click(validation);
    expect(openInspector).toHaveBeenCalledExactlyOnceWith("validation");
    await user.click(audit);
    expect(openInspector).toHaveBeenLastCalledWith("audit");
  });

  /* WCAG 2.5.3 Label in Name, as a COMPUTED property rather than a declaration:
     the pill's visible string must be contained in its accessible name, so a
     speech-input user saying what they see hits the control. Before
     elspeth-a41fd9d32b the colon lived only in the accessible name, so
     "Validation Passed" was not contained in "Validation: Passed". Whitespace
     is stripped on both sides because the separator between the two spans is a
     flex gap in the render and a stripped inter-element newline in textContent
     — the punctuation is what this pins, not the spacing. */
  it.each([
    [
      "terminal pass",
      (): void => {
        useExecutionStore.setState({ validationResult: makeValidationResult() });
        useAuditReadinessStore.setState({
          snapshotsBySession: { "session-1": freshAuditSnapshot() },
        });
      },
      ["Validation: Passed", "Audit: Ready"],
    ],
    [
      "both in flight",
      (): void => {
        useExecutionStore.setState({ isValidating: true } as never);
      },
      ["Validation: Checking", "Audit: Checking"],
    ],
    [
      "counted errors",
      (): void => {
        useExecutionStore.setState({
          validationResult: makeValidationResult({
            is_valid: false,
            errors: [
              {
                component_id: "step-1",
                component_type: "transform",
                message: "first",
                suggestion: null,
              },
              {
                component_id: "step-2",
                component_type: "transform",
                message: "second",
                suggestion: null,
              },
            ],
          }),
        });
      },
      ["Validation: 2 errors", "Audit: Checking"],
    ],
    [
      "terminal failure on both",
      (): void => {
        useExecutionStore.setState({
          validationError: "upstream said no",
        } as never);
        useAuditReadinessStore.setState({
          snapshotsBySession: { "session-1": freshAuditSnapshot() },
          errorBySession: { "session-1": "upstream said no" },
        });
      },
      ["Validation: Check failed", "Audit: Error"],
    ],
  ] as const)(
    "keeps each status pill's visible label inside its accessible name (%s)",
    (_scenario, arrange, expectedNames) => {
      arrange();
      const { container } = renderActionBar({
        completion: false,
      });

      const pills = [
        ...container.querySelectorAll<HTMLButtonElement>(
          ".workspace-status-control",
        ),
      ];
      expect(pills).toHaveLength(expectedNames.length);
      for (const [index, expectedName] of expectedNames.entries()) {
        // getByRole resolves the real accessible-name computation, not the
        // attribute we wrote, so a regression that drops aria-label is caught
        // here rather than passing on a self-consistent attribute read.
        expect(screen.getByRole("button", { name: expectedName })).toBe(
          pills[index],
        );
        expect(expectedName.replace(/\s+/g, "")).toBe(
          (pills[index].textContent ?? "").replace(/\s+/g, ""),
        );
      }
    },
  );

  it("shows audit as Error when the current readiness request has failed identity validation", () => {
    useAuditReadinessStore.setState({
      snapshotsBySession: { "session-1": freshAuditSnapshot() },
      errorBySession: {
        "session-1":
          "Audit readiness response did not match the requested composition.",
      },
    });

    renderActionBar({ completion: false });

    expect(
      screen.getByRole("button", { name: "Audit: Error" }),
    ).toHaveTextContent("Audit:Error");
    expect(
      screen.queryByRole("button", { name: "Audit: Ready" }),
    ).not.toBeInTheDocument();
  });

  it("shows validation request progress and a sanitised terminal failure", () => {
    useExecutionStore.setState({
      validationResult: null,
      isValidating: true,
      validationError: null,
    } as never);
    const view = renderActionBar({
      completion: false,
    });
    expect(
      screen.getByRole("button", { name: "Validation: Checking" }),
    ).toHaveAttribute("data-tone", "busy");

    act(() => {
      useExecutionStore.setState({
        isValidating: false,
        validationError: "sensitive upstream validation response",
      } as never);
    });
    expect(
      screen.getByRole("button", { name: "Validation: Check failed" }),
    ).toHaveTextContent("Validation:Check failed");
    expect(view.container).not.toHaveTextContent(
      "sensitive upstream validation response",
    );
  });

  it.each([
    ["ordinary freeform", true],
    ["terminal guided", true],
    ["active guided", false],
    ["tutorial", false],
  ] as const)(
    "pins the %s capability matrix",
    (_mode, completion) => {
      renderActionBar({ completion });

      expect(screen.queryByTestId("completion-bar") !== null).toBe(completion);
      // The More-actions popover retired 2026-08-15 (its last item, the
      // Plugin catalog, moved to the artifact-workspace toolbar); the bar
      // must never grow it back.
      expect(
        screen.queryByRole("button", { name: "More actions" }),
      ).toBeNull();
      expect(
        screen.getByRole("button", { name: /Validation:/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /Audit:/ }),
      ).toBeInTheDocument();
    },
  );

});
