import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import { useSessionStore } from "@/stores/sessionStore";
import { ChecksView } from "./ChecksView";

vi.mock("@/components/audit/AuditReadinessPanel", () => ({
  AuditReadinessPanel: ({
    onSelectComponent,
  }: {
    onSelectComponent?: (componentId: string) => void;
  }) => (
    <div>
      Audit panel
      <button
        type="button"
        onClick={() => onSelectComponent?.("select_columns")}
      >
        Audit component
      </button>
    </div>
  ),
}));

vi.mock("@/components/sidebar/SideRailValidationBanner", () => ({
  SideRailValidationBanner: () => <div>Validation panel</div>,
}));

describe("ChecksView", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: makeComposition(4),
      selectNode: vi.fn(),
    } as never);
  });

  it("renders both check surfaces with the default validation owner", () => {
    render(<ChecksView />);
    expect(screen.getByText("Validation panel")).toBeInTheDocument();
    expect(screen.getByText("Audit panel")).toBeInTheDocument();
  });

  it("names the validation half as its own landmark, mirroring the audit panel's self-label", () => {
    // The retired Inspector gave this content an implicit "Validation"
    // tabpanel name; SideRailValidationBanner itself renders no heading or
    // label, so without a named wrapper a landmark-navigating screen-reader
    // user finds "Audit readiness" but nothing named "Validation" before it.
    render(<ChecksView />);
    const region = screen.getByRole("region", { name: "Validation" });
    expect(within(region).getByText("Validation panel")).toBeInTheDocument();
  });

  it("uses injected tutorial validation content without mounting the default owner", () => {
    render(
      <ChecksView
        validationContent={
          <div data-testid="tutorial-validation">Tutorial validation</div>
        }
      />,
    );
    const region = screen.getByRole("region", { name: "Validation" });
    expect(
      within(region).getByTestId("tutorial-validation"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Validation panel")).toBeNull();
  });

  it("routes component selection to the store and the Graph tab", async () => {
    // Ported from the retired WorkspaceInspector selectComponent contract:
    // selecting a component selects the node and requests the Graph artifact
    // view WITHOUT focus mode (no modal).
    const selectNode = vi.fn();
    useSessionStore.setState({ selectNode } as never);
    const artifactRequests: RequestArtifactViewDetail[] = [];
    const onArtifactRequest = (event: Event) => {
      artifactRequests.push(
        (event as CustomEvent<RequestArtifactViewDetail>).detail,
      );
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    try {
      render(<ChecksView />);
      await userEvent
        .setup()
        .click(screen.getByRole("button", { name: "Audit component" }));

      expect(selectNode).toHaveBeenCalledExactlyOnceWith("select_columns");
      expect(artifactRequests).toEqual([
        { tab: "graph", focusMode: false, sessionId: "session-1" },
      ]);
    } finally {
      window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    }
  });
});
