import { render, screen } from "@testing-library/react";
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

vi.mock("@/components/sidebar/ImportYamlButton", () => ({
  ImportYamlButton: () => <button type="button">Import YAML</button>,
}));

vi.mock("@/components/sidebar/CatalogButton", () => ({
  CatalogButton: () => <button type="button">Plugin catalog</button>,
}));

function renderActionBar(
  capabilities: {
    completion: boolean;
    importYaml: boolean;
    catalog: boolean;
  },
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

  it("always renders text-labelled validation and audit inspector controls", async () => {
    const user = userEvent.setup();
    useExecutionStore.setState({ validationResult: makeValidationResult() });
    useAuditReadinessStore.setState({
      snapshotsBySession: { "session-1": freshAuditSnapshot() },
    });
    const { openInspector, container } = renderActionBar({
      completion: false,
      importYaml: false,
      catalog: false,
    });

    const actionBar = container.querySelector(".workspace-action-bar");
    expect(actionBar).not.toBeNull();
    const statusGroup = screen.getByRole("group", { name: "Workspace status" });
    expect(statusGroup).toHaveAttribute("data-workspace-status-controls", "true");
    const validation = screen.getByRole("button", {
      name: "Validation: Passed",
    });
    const audit = screen.getByRole("button", { name: "Audit: Ready" });
    expect(validation).toHaveTextContent("ValidationPassed");
    expect(audit).toHaveTextContent("AuditReady");

    await user.click(validation);
    expect(openInspector).toHaveBeenCalledExactlyOnceWith("validation");
    await user.click(audit);
    expect(openInspector).toHaveBeenLastCalledWith("audit");
  });

  it("shows audit as Error when the current readiness request has failed identity validation", () => {
    useAuditReadinessStore.setState({
      snapshotsBySession: { "session-1": freshAuditSnapshot() },
      errorBySession: {
        "session-1":
          "Audit readiness response did not match the requested composition.",
      },
    });

    renderActionBar({ completion: false, importYaml: false, catalog: false });

    expect(
      screen.getByRole("button", { name: "Audit: Error" }),
    ).toHaveTextContent("AuditError");
    expect(
      screen.queryByRole("button", { name: "Audit: Ready" }),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["ordinary freeform", true, true, true],
    ["terminal guided", true, true, true],
    ["active guided", false, false, false],
    ["tutorial", false, false, false],
  ] as const)(
    "pins the %s capability matrix",
    (_mode, completion, importYaml, catalog) => {
      renderActionBar({ completion, importYaml, catalog });

      expect(screen.queryByTestId("completion-bar") !== null).toBe(completion);
      expect(
        screen.queryByRole("button", { name: "More actions" }) !== null,
      ).toBe(importYaml || catalog);
      expect(
        screen.getByRole("button", { name: /Validation:/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /Audit:/ }),
      ).toBeInTheDocument();
    },
  );

  it("renders only individually admitted secondary actions", async () => {
    const user = userEvent.setup();
    renderActionBar({ completion: false, importYaml: true, catalog: false });
    await user.click(screen.getByRole("button", { name: "More actions" }));

    expect(
      screen.getByRole("button", { name: "Import YAML" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Plugin catalog" }),
    ).not.toBeInTheDocument();
  });

  it("operates the secondary menu by Arrow, Home, End, and Escape keys", async () => {
    const user = userEvent.setup();
    renderActionBar({ completion: false, importYaml: true, catalog: true });
    const more = screen.getByRole("button", { name: "More actions" });
    expect(more).not.toHaveAttribute("aria-haspopup");
    expect(more).toHaveAttribute(
      "aria-controls",
      "workspace-more-actions-panel",
    );
    more.focus();

    await user.keyboard("{ArrowDown}");
    const importYaml = screen.getByRole("button", { name: "Import YAML" });
    const catalog = screen.getByRole("button", { name: "Plugin catalog" });
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("group", { name: "More actions" })).toHaveAttribute(
      "id",
      "workspace-more-actions-panel",
    );
    expect(importYaml).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(catalog).toHaveFocus();
    await user.keyboard("{Home}");
    expect(importYaml).toHaveFocus();
    await user.keyboard("{End}");
    expect(catalog).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(more).toHaveFocus();
    expect(
      screen.queryByRole("button", { name: "Import YAML" }),
    ).not.toBeInTheDocument();
  });
});
