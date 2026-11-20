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
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
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
    // elspeth-5413e4221e lineage: the strip used to mix two registers. The
    // status chips retired to the Checks tab, and on 2026-11-20 the pane
    // toggle moved to the title bars, so only workflow actions remain and
    // they take the .btn register. Asserted as an EMPTY set so a future
    // strip rule cannot silently reintroduce a second register
    // (min-height), and so the retired toggle classes stay gone.
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");

    const barRegisters = new Map<string, string>();
    for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = rule[1].trim();
      if (
        !/\.workspace-(action-bar|status-control|pane-controls|collapse-control|restore-control|collapsed-affordance)\b/.test(
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

    expect([...barRegisters.entries()]).toEqual([]);
    expect(css).not.toMatch(
      /\.workspace-(pane-controls|collapse-control|restore-control|collapsed-affordance)\b/,
    );
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
