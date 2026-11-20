import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { BlobMetadata, CompositionState } from "@/types/api";
import { FileWiringPad } from "./FileWiringPad";

const blob = { id: "new-file", filename: "prompt.txt", content_hash: "new-hash" } as BlobMetadata;
const state = {
  nodes: [
    { id: "single", plugin: "llm", options: { prompt_template: "Keep this text verbatim." } },
    { id: "multi", plugin: "llm", options: { queries: [
      { name: "score", template: { blob_ref: "deleted-file", mode: "inline_content", sha256: "old-hash" } },
      { name: "rationale", template: "Do not change the rationale." },
    ] } },
    { id: "mapping", plugin: "llm", options: { queries: { comments: { template: "Existing comments." } } } },
    { id: "other", plugin: "passthrough", options: { prompt_template: "Not an LLM prompt." } },
  ],
} as unknown as CompositionState;

function setup(disabledReason: string | null = null) {
  const onConnect = vi.fn();
  const onClose = vi.fn();
  render(<FileWiringPad blob={blob} state={state} blobs={[blob]} disabledReason={disabledReason} onConnect={onConnect} onClose={onClose} />);
  return { onConnect, onClose, user: userEvent.setup() };
}

describe("FileWiringPad", () => {
  it("lists named multi-prompt slots and submits only the owner's selected connections", async () => {
    const { onConnect, user } = setup();
    expect(screen.queryByText(/other —/)).not.toBeInTheDocument();
    expect(screen.getByText("Missing file")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /multi — User prompt, query score/ }));
    await user.click(screen.getByRole("checkbox", { name: /mapping — User prompt, query comments/ }));
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    expect(onConnect).toHaveBeenCalledOnce();
    const request = onConnect.mock.calls[0][0];
    expect(request).toContain('"blob_id":"new-file"');
    expect(request).toContain('"node:multi.options.queries.0.template"');
    expect(request).toContain('"node:mapping.options.queries.comments.template"');
    expect(request).not.toContain("queries.1.template");
    expect(request).not.toContain("Keep this text verbatim.");
    expect(request).not.toContain("Do not change the rationale.");
    expect(request).toContain("Keep file references in the graph");
  });

  it("lets the owner describe the connection for AI to choose", async () => {
    const { onConnect, user } = setup();
    await user.click(screen.getByRole("radio", { name: "Ask AI" }));
    expect(screen.getByRole("button", { name: "Ask AI to connect" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "What should AI connect?" }), "Use this file for the scoring prompt.");
    await user.click(screen.getByRole("button", { name: "Ask AI to connect" }));
    expect(onConnect).toHaveBeenCalledWith(expect.stringContaining("Use this file for the scoring prompt."));
    expect(onConnect).toHaveBeenCalledWith(expect.stringContaining('"blob_id":"new-file"'));
  });

  it("does not submit while the session, graph or file is unavailable", async () => {
    const { onConnect, user } = setup("The graph changed. Close the wiring pad and reopen it.");
    await user.click(screen.getByRole("checkbox", { name: /single — User prompt/ }));
    const connect = screen.getByRole("button", { name: "Connect selected prompts" });
    expect(connect).toBeDisabled();
    await user.click(connect);
    expect(onConnect).not.toHaveBeenCalled();
  });

  it("keeps an existing connection selected only when its hash matches the current file", () => {
    const current = { nodes: [{ id: "llm", plugin: "llm", options: {
      system_prompt: { blob_ref: "NEW-FILE", mode: "inline_content", sha256: "new-hash" },
      prompt_template: { blob_ref: "new-file", mode: "inline_content", sha256: "old-hash" },
    } }] } as unknown as CompositionState;
    render(<FileWiringPad blob={blob} state={current} blobs={[blob]} disabledReason={null} onConnect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: /System prompt/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /System prompt/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /User prompt/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /User prompt/ })).toBeEnabled();
  });

  it("closes on Escape without requesting a connection", async () => {
    const { onConnect, onClose, user } = setup();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    expect(onConnect).not.toHaveBeenCalled();
  });
});
