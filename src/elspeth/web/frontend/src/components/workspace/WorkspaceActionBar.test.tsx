import { readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "@/stores/sessionStore";
import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import { WorkspaceActionBar } from "./WorkspaceActionBar";
import { WorkspacePaneProvider } from "./WorkspacePaneContext";
import type { WorkspacePaneState } from "./useWorkspacePaneState";

vi.mock("@/components/composer/CompletionBar", () => ({
  CompletionBar: () => <div data-testid="completion-bar">Completion actions</div>,
}));

function renderActionBar(
  capabilities: { completion: boolean },
) {
  const paneState: WorkspacePaneState = {
    paneBounds: { min: 360, max: 640, defaultWidth: 420, resizable: true },
    preferredAuthoringWidth: 420,
    effectiveAuthoringWidth: 420,
    authoringCollapsed: false,
    availableArtifactTabs: ["graph", "spec", "yaml", "checks", "run"],
    activeArtifactTab: "graph",
    activeInspectorTab: null,
    inspectorOpen: false,
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
    openInspector: vi.fn(),
    closeInspector: vi.fn(),
  };
  return render(
    <WorkspacePaneProvider paneState={paneState}>
      <WorkspaceActionBar capabilities={capabilities} />
    </WorkspacePaneProvider>,
  );
}

describe("WorkspaceActionBar", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: makeComposition(4),
    } as never);
  });

  it("keeps every control in the action bar on one height register", () => {
    // elspeth-5413e4221e lineage: the strip used to mix two registers. With
    // the status chips retired to the Checks tab, the collapse control across
    // the pane divider is the register's remaining owner — asserted as a SET
    // so a future action-bar rule cannot silently reintroduce a second
    // register (min-height) in this strip.
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
    ]);
    expect([...new Set(barRegisters.values())]).toEqual([
      "var(--size-control)",
    ]);
  });

  it("renders no status chips — the Checks tab owns the ambient status", () => {
    renderActionBar({ completion: true });

    expect(
      screen.queryByRole("button", { name: /^Validation: / }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /^Audit: / })).toBeNull();
    expect(
      document.querySelector(".workspace-status-controls"),
    ).toBeNull();
  });

  it("mounts the completion bar only when the capability is granted", () => {
    const { unmount } = renderActionBar({ completion: true });
    expect(screen.getByTestId("completion-bar")).toBeInTheDocument();
    unmount();

    renderActionBar({ completion: false });
    expect(screen.queryByTestId("completion-bar")).toBeNull();
  });
});
