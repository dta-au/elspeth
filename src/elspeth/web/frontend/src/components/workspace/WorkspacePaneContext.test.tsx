import { render, screen } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  WorkspacePaneContextError,
  WorkspacePaneProvider,
  useWorkspacePaneController,
} from "./WorkspacePaneContext";
import type { WorkspacePaneState } from "./useWorkspacePaneState";

function makePaneState(
  overrides: Partial<WorkspacePaneState> = {},
): WorkspacePaneState {
  return {
    paneBounds: {
      min: 360,
      max: 640,
      defaultWidth: 420,
      resizable: true,
    },
    preferredAuthoringWidth: 420,
    effectiveAuthoringWidth: 420,
    authoringCollapsed: false,
    availableArtifactTabs: ["graph", "spec", "yaml", "run"],
    activeArtifactTab: "graph",
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
    ...overrides,
  };
}

describe("WorkspacePaneContext", () => {
  it("throws a named actionable developer error outside its provider", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const preventExpectedWindowError = (event: ErrorEvent) => {
      event.preventDefault();
    };
    window.addEventListener("error", preventExpectedWindowError);

    function MissingProviderConsumer() {
      useWorkspacePaneController();
      return null;
    }

    let caught: unknown = null;
    try {
      render(<MissingProviderConsumer />);
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(WorkspacePaneContextError);
    expect(caught).toHaveProperty(
      "message",
      expect.stringMatching(/must be rendered inside ComposerWorkspace/i),
    );
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });

  it("publishes Task 1 actions by identity and keeps action access stable", () => {
    const paneState = makePaneState();
    const observations: ReturnType<typeof useWorkspacePaneController>[] = [];

    function Consumer() {
      const controller = useWorkspacePaneController();
      useLayoutEffect(() => {
        observations.push(controller);
      }, [controller]);
      return <output>{controller.state.activeArtifactTab}</output>;
    }

    const view = render(
      <WorkspacePaneProvider paneState={paneState}>
        <Consumer />
      </WorkspacePaneProvider>,
    );
    view.rerender(
      <WorkspacePaneProvider
        paneState={{ ...paneState, effectiveAuthoringWidth: 500 }}
      >
        <Consumer />
      </WorkspacePaneProvider>,
    );

    expect(screen.getByText("graph")).toBeInTheDocument();
    expect(observations).toHaveLength(1);
    const actions = observations[0].actions;
    expect(actions.resizeTransient).toBe(paneState.resizeTransient);
    expect(actions.commitResize).toBe(paneState.commitResize);
    expect(actions.setAuthoringCollapsed).toBe(
      paneState.setAuthoringCollapsed,
    );
    expect(actions.selectArtifactTab).toBe(paneState.selectArtifactTab);
  });

});
