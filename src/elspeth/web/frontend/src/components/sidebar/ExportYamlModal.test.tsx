import { beforeEach, describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ExportYamlModal } from "./ExportYamlModal";
import { OPEN_YAML_MODAL_EVENT } from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import { makeComposition } from "@/test/composerFixtures";
import * as api from "@/api/client";

vi.mock("@/api/client", () => ({
  fetchYaml: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

beforeEach(() => {
  useSessionStore.setState({
    activeSessionId: "session-1",
    compositionState: makeComposition(1),
    compositionProposals: [],
    proposalActionPendingIds: [],
    staleProposalIds: [],
    exportedYamlBlobBinding: null,
  });
  vi.mocked(api.fetchYaml).mockReset().mockResolvedValue({
    yaml: "sources:\n  source:\n    plugin: csv_file\n",
  });
});

describe("ExportYamlModal", () => {
  it("renders nothing until opened", () => {
    const { container } = render(<ExportYamlModal />);
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it("opens on OPEN_YAML_MODAL_EVENT", async () => {
    render(<ExportYamlModal />);

    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    expect(
      screen.getByRole("dialog", { name: /export yaml/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: /review yaml/i }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Copy YAML to clipboard" }),
    ).toBeInTheDocument();
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
  // the close button receives focus on open. Tab cycles through the real YAML
  // controls and Shift+Tab wraps back — verifying the trap does not leak focus
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
    const copyBtn = await screen.findByRole("button", {
      name: "Copy YAML to clipboard",
    });

    closeBtn.focus();
    await userEvent.tab();
    expect(copyBtn).toHaveFocus();

    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: "Download YAML file" }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(closeBtn).toHaveFocus();
  });

  it("traps Shift+Tab backward through focusable elements without escaping the modal", async () => {
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    const closeBtn = screen.getByRole("button", { name: /close export yaml/i });
    const downloadBtn = await screen.findByRole("button", {
      name: "Download YAML file",
    });

    closeBtn.focus();
    await userEvent.tab({ shift: true });
    expect(downloadBtn).toHaveFocus();

    await userEvent.tab({ shift: true });
    expect(
      screen.getByRole("button", { name: "Copy YAML to clipboard" }),
    ).toHaveFocus();
  });
});

describe("ExportYamlModal — YamlView note ownership", () => {
  it("adds no modal-owned notes while real YamlView supplies exactly one scope note", async () => {
    const yaml = deferred<{ yaml: string }>();
    vi.mocked(api.fetchYaml).mockReturnValue(yaml.promise);
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    expect(
      screen.queryByTestId("yaml-export-scope-note"),
    ).not.toBeInTheDocument();

    await act(async () => {
      yaml.resolve({ yaml: "sources: {}\n" });
      await yaml.promise;
    });
    expect(screen.getAllByTestId("yaml-export-scope-note")).toHaveLength(1);
    expect(screen.queryByTestId("yaml-export-blob-note")).not.toBeInTheDocument();
  });

  it("lets real YamlView supply one conditional blob note", async () => {
    vi.mocked(api.fetchYaml).mockResolvedValue({
      yaml: "sources: {}\n",
      source_blob_ids: { source: "blob-1" },
    });
    render(<ExportYamlModal />);
    fireEvent(window, new CustomEvent(OPEN_YAML_MODAL_EVENT));

    expect(await screen.findAllByTestId("yaml-export-scope-note")).toHaveLength(1);
    expect(screen.getAllByTestId("yaml-export-blob-note")).toHaveLength(1);
  });
});
