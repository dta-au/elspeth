import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GraphChecksum } from "./GraphChecksum";

const HASH = "0123456789abcdef".repeat(4);
const OTHER_HASH = "fedcba9876543210".repeat(4);
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");

function clipboard(value: Partial<Pick<Clipboard, "readText" | "writeText">> | undefined) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
});

describe("GraphChecksum", () => {
  it("shows the complete server digest without reading or writing the clipboard on render", () => {
    const readText = vi.fn();
    const writeText = vi.fn();
    clipboard({ readText, writeText });
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    expect(screen.getByLabelText("Graph content checksum")).toHaveTextContent(HASH);
    expect(screen.getByRole("region", { name: "Graph checksum" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy checksum" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Match clipboard" })).toBeEnabled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(readText).not.toHaveBeenCalled();
    expect(writeText).not.toHaveBeenCalled();
  });

  it("marks a missing server digest unavailable without synthesizing one", () => {
    render(<GraphChecksum contentHash={undefined} stateIdentity="session-a/state-1/v1" />);
    expect(screen.getByText("Checksum unavailable for this graph.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Graph content checksum")).not.toBeInTheDocument();
  });

  it("copies exactly the server digest and announces completion", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard({ writeText });
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    await user.click(screen.getByRole("button", { name: "Copy checksum" }));

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(HASH);
    expect(screen.getByRole("status")).toHaveTextContent("Checksum copied.");
  });

  it.each([
    [HASH, "Checksum matches this graph.", "check"],
    [`  ${HASH.toUpperCase()}\n`, "Checksum matches this graph.", "check"],
    [OTHER_HASH, "Checksum does not match this graph.", "cross"],
    ["  ", "No checksum to compare.", null],
    ["a".repeat(63), "Invalid checksum.", null],
    ["g".repeat(64), "Invalid checksum.", null],
  ] as const)("compares clipboard text %j with distinct outcomes", async (value, message, icon) => {
    const user = userEvent.setup();
    const readText = vi.fn().mockResolvedValue(value);
    clipboard({ readText });
    const { container } = render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    await user.click(screen.getByRole("button", { name: "Match clipboard" }));

    expect(readText).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(message);
    if (icon) {
      expect(container.querySelector(`[data-icon="${icon}"]`)).toHaveAttribute("aria-hidden", "true");
    } else {
      expect(container.querySelector("[data-icon]")).toBeNull();
    }
  });

  it("distinguishes denied reading and provides keyboard-operable manual comparison", async () => {
    const user = userEvent.setup();
    const readText = vi.fn().mockRejectedValue(new DOMException("Denied", "NotAllowedError"));
    clipboard({ readText });
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);
    await user.click(screen.getByRole("button", { name: "Match clipboard" }));

    expect(screen.getByRole("status")).toHaveTextContent("Clipboard access denied.");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    const paste = screen.getByRole("button", { name: "Paste checksum" });
    paste.focus();
    await user.keyboard("{Enter}");
    const input = screen.getByRole("textbox", { name: "Checksum to compare" });
    expect(input).toHaveFocus();
    await user.type(input, HASH);
    await user.keyboard("{Enter}");

    expect(screen.getByRole("status")).toHaveTextContent("Checksum matches this graph.");
    expect(readText).toHaveBeenCalledTimes(1);
    await user.type(input, "f");
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("distinguishes unavailable clipboard APIs for read and copy", async () => {
    const user = userEvent.setup();
    clipboard(undefined);
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    await user.click(screen.getByRole("button", { name: "Match clipboard" }));
    expect(screen.getByRole("status")).toHaveTextContent("Clipboard reading is unavailable.");
    await user.click(screen.getByRole("button", { name: "Copy checksum" }));
    expect(screen.getByRole("status")).toHaveTextContent("Clipboard copying is unavailable.");
    expect(screen.getByLabelText("Graph content checksum")).toHaveTextContent(HASH);
  });

  it.each([
    ["NotAllowedError", "Clipboard access denied."],
    ["UnknownError", "Could not copy the checksum."],
  ])("reports %s while copying without claiming success", async (name, message) => {
    const user = userEvent.setup();
    clipboard({ writeText: vi.fn().mockRejectedValue(new DOMException("Failed", name)) });
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    await user.click(screen.getByRole("button", { name: "Copy checksum" }));
    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.getByRole("status")).not.toHaveTextContent("Checksum copied.");
  });

  it("reports a failed clipboard read separately from mismatch", async () => {
    const user = userEvent.setup();
    clipboard({ readText: vi.fn().mockRejectedValue(new Error("Unavailable transport")) });
    render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);

    await user.click(screen.getByRole("button", { name: "Match clipboard" }));
    expect(screen.getByRole("status")).toHaveTextContent("Could not read the clipboard.");
    expect(screen.getByRole("status")).not.toHaveTextContent("does not match");
  });

  it.each([
    [OTHER_HASH, "session-a/state-1/v1"],
    [HASH, "session-a/state-2/v2"],
    [HASH, "session-b/state-1/v1"],
  ])("clears comparison and manual text when hash or saved state identity changes", async (hash, identity) => {
    const user = userEvent.setup();
    const { rerender } = render(<GraphChecksum contentHash={HASH} stateIdentity="session-a/state-1/v1" />);
    await user.click(screen.getByRole("button", { name: "Paste checksum" }));
    await user.type(screen.getByRole("textbox", { name: "Checksum to compare" }), HASH);
    await user.click(screen.getByRole("button", { name: "Compare checksum" }));
    expect(screen.getByRole("status")).toHaveTextContent("Checksum matches this graph.");

    rerender(<GraphChecksum contentHash={hash} stateIdentity={identity} />);

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("discards a pending clipboard match through A → B → A", async () => {
    const user = userEvent.setup();
    let finishRead!: (value: string) => void;
    const pending = new Promise<string>((resolve) => { finishRead = resolve; });
    clipboard({ readText: vi.fn().mockReturnValue(pending) });
    const { rerender } = render(<GraphChecksum contentHash={HASH} stateIdentity="a" />);
    await user.click(screen.getByRole("button", { name: "Match clipboard" }));
    expect(screen.getByRole("status")).toHaveTextContent("Reading clipboard…");
    expect(screen.getByRole("button", { name: "Copy checksum" })).toBeDisabled();

    rerender(<GraphChecksum contentHash={OTHER_HASH} stateIdentity="b" />);
    rerender(<GraphChecksum contentHash={HASH} stateIdentity="a" />);
    await act(async () => { finishRead(HASH); await pending; });

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByRole("button", { name: "Match clipboard" })).toBeEnabled();
  });

  it("discards pending copy feedback after a saved-state change", async () => {
    const user = userEvent.setup();
    let finishCopy!: () => void;
    const pending = new Promise<void>((resolve) => { finishCopy = resolve; });
    clipboard({ writeText: vi.fn().mockReturnValue(pending) });
    const { rerender } = render(<GraphChecksum contentHash={HASH} stateIdentity="a" />);
    await user.click(screen.getByRole("button", { name: "Copy checksum" }));

    rerender(<GraphChecksum contentHash={HASH} stateIdentity="b" />);
    await act(async () => { finishCopy(); await pending; });

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("does not let an earlier clipboard read replace a later manual comparison", async () => {
    const user = userEvent.setup();
    let finishRead!: (value: string) => void;
    const pending = new Promise<string>((resolve) => { finishRead = resolve; });
    clipboard({ readText: vi.fn().mockReturnValue(pending) });
    render(<GraphChecksum contentHash={HASH} stateIdentity="a" />);
    await user.click(screen.getByRole("button", { name: "Match clipboard" }));
    await user.click(screen.getByRole("button", { name: "Paste checksum" }));
    await user.type(screen.getByRole("textbox", { name: "Checksum to compare" }), OTHER_HASH);
    await user.click(screen.getByRole("button", { name: "Compare checksum" }));

    await act(async () => { finishRead(HASH); await pending; });

    expect(screen.getByRole("status")).toHaveTextContent("Checksum does not match this graph.");
  });
});
