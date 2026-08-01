import { beforeEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ExportYamlModal } from "./ExportYamlModal";
import { OPEN_YAML_MODAL_EVENT } from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";

// The stub must include a focusable element so the focus trap has two nodes
// (Close button + stub button) and the wrap-around logic is exercised. A
// stub with zero focusable children would produce a single-element cycle that
// passes trivially even without useFocusTrap wired.
vi.mock("@/components/inspector/YamlView", () => ({
  YamlView: () => (
    <button type="button" data-testid="yaml-view-stub">
      stub
    </button>
  ),
}));

describe("ExportYamlModal", () => {
  it("renders nothing until opened", () => {
    const { container } = render(<ExportYamlModal />);
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it("opens on OPEN_YAML_MODAL_EVENT", () => {
    render(<ExportYamlModal />);

    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    expect(
      screen.getByRole("dialog", { name: /export yaml/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: /review yaml/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("yaml-view-stub")).toBeInTheDocument();
  });

  it("closes on Escape", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes when the backdrop is clicked", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    fireEvent.click(screen.getByTestId("yaml-modal-backdrop"));

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes when the close button is clicked", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    fireEvent.click(screen.getByRole("button", { name: /close export yaml/i }));

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // ── Focus trap ──────────────────────────────────────────────────────────────
  // useFocusTrap is wired with initialFocusSelector ".yaml-modal-close" so
  // the close button receives focus on open.  Tab cycles forward to the stub
  // button and Shift+Tab wraps back — verifying the trap does not leak focus
  // outside the modal.

  it("moves focus to the close button when the modal opens", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    expect(
      screen.getByRole("button", { name: /close export yaml/i }),
    ).toHaveFocus();
  });

  it("traps Tab forward through focusable elements without escaping the modal", async () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    const closeBtn = screen.getByRole("button", { name: /close export yaml/i });
    const stubBtn = screen.getByTestId("yaml-view-stub");

    closeBtn.focus();
    await userEvent.tab();
    expect(stubBtn).toHaveFocus();

    // Wraps back to first — focus stays inside the modal
    await userEvent.tab();
    expect(closeBtn).toHaveFocus();
  });

  it("traps Shift+Tab backward through focusable elements without escaping the modal", async () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    const closeBtn = screen.getByRole("button", { name: /close export yaml/i });
    const stubBtn = screen.getByTestId("yaml-view-stub");

    closeBtn.focus();
    await userEvent.tab({ shift: true });
    // Wraps from first to last
    expect(stubBtn).toHaveFocus();

    await userEvent.tab({ shift: true });
    expect(closeBtn).toHaveFocus();
  });
});

// elspeth-8d17056117: exported YAML is not standalone-runnable, and the modal
// said nothing about it. Two omissions with different evidence:
//   * blob-bound source paths — a REAL signal exists (fetchYaml's
//     source_blob_ids, stored as exportedYamlBlobBinding), so that line is
//     conditional on it;
//   * the landscape: block — no signal exists at all (yaml_importer never
//     reads the key, so nothing records that one was dropped), so the scope
//     note is unconditional and states what is true of every export.
describe("ExportYamlModal — export scope note", () => {
  beforeEach(() => {
    useSessionStore.setState({
      activeSessionId: "s1",
      exportedYamlBlobBinding: null,
    } as never);
  });

  it("always states that the export carries the pipeline definition only", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));
    expect(screen.getByTestId("yaml-export-scope-note")).toHaveTextContent(
      /pipeline definition/i,
    );
    expect(screen.getByTestId("yaml-export-scope-note")).toHaveTextContent(
      /landscape/i,
    );
  });

  it("omits the session-bound source line when no source is blob-backed", () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));
    expect(
      screen.queryByTestId("yaml-export-blob-note"),
    ).not.toBeInTheDocument();
  });

  it("warns that source data is session-bound when a source is blob-backed", () => {
    useSessionStore.setState({
      exportedYamlBlobBinding: {
        sessionId: "s1",
        yaml: "sources: {}",
        sourceBlobIds: { source: "blob-1" },
      },
    } as never);
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));
    expect(screen.getByTestId("yaml-export-blob-note")).toHaveTextContent(
      /session-bound/i,
    );
  });

  // The binding outlives the export that produced it: YamlView clears yaml and
  // yamlError before each fetch but never the binding, and its error path
  // leaves it untouched entirely — so a 409 (validation-blocked export) would
  // otherwise strand the warning on a pipeline it does not describe.
  it("ignores a binding left behind by a different session", () => {
    useSessionStore.setState({
      activeSessionId: "s2",
      exportedYamlBlobBinding: {
        sessionId: "s1",
        yaml: "sources: {}",
        sourceBlobIds: { source: "blob-1" },
      },
    } as never);
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));
    expect(
      screen.queryByTestId("yaml-export-blob-note"),
    ).not.toBeInTheDocument();
  });
});
