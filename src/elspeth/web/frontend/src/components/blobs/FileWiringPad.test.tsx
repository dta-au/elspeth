import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
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

function setup(disabledReason: string | null = null, sendRefusal: { message: string; seq: number } | null = null) {
  const onConnect = vi.fn();
  const onClose = vi.fn();
  render(<FileWiringPad blob={blob} state={state} blobs={[blob]} disabledReason={disabledReason} sendRefusal={sendRefusal}
    onConnect={onConnect} onClose={onClose} />);
  return { onConnect, onClose, user: userEvent.setup() };
}

const step = (name: string) => screen.getByRole("group", { name });

describe("FileWiringPad", () => {
  it("lists named multi-prompt slots and submits only the owner's selected connections", async () => {
    const { onConnect, user } = setup();
    expect(screen.queryByRole("group", { name: "other" })).not.toBeInTheDocument();
    await user.click(within(step("multi")).getByRole("checkbox", { name: /User prompt, query score/ }));
    await user.click(within(step("mapping")).getByRole("checkbox", { name: /User prompt, query comments/ }));
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

  it("lets the owner describe the connection for Composer to choose", async () => {
    const { onConnect, user } = setup();
    await user.click(screen.getByRole("radio", { name: "Ask Composer" }));
    expect(screen.getByRole("button", { name: "Ask Composer to connect" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "What should Composer connect?" }), "Use this file for the scoring prompt.");
    await user.click(screen.getByRole("button", { name: "Ask Composer to connect" }));
    expect(onConnect).toHaveBeenCalledWith(expect.stringContaining("Use this file for the scoring prompt."));
    expect(onConnect).toHaveBeenCalledWith(expect.stringContaining('"blob_id":"new-file"'));
  });

  it("does not submit while the session, graph or file is unavailable", async () => {
    const { onConnect, user } = setup("The graph changed. Close the wiring pad and reopen it.");
    await user.click(within(step("single")).getByRole("checkbox", { name: /User prompt/ }));
    const connect = screen.getByRole("button", { name: "Connect selected prompts" });
    expect(connect).toBeDisabled();
    // The reason sits in the footer beside the button, as a status the button names as its description.
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("The graph changed. Close the wiring pad and reopen it.");
    expect(status.closest("footer")).toBe(connect.closest("footer"));
    expect(connect).toHaveAccessibleDescription("The graph changed. Close the wiring pad and reopen it.");
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

  it("says what each connection replaces", () => {
    const other = { id: "other-file", filename: "rubric.txt", content_hash: "h" } as BlobMetadata;
    const current = { nodes: [{ id: "llm", plugin: "llm", options: {
      system_prompt: { blob_ref: "other-file", mode: "inline_content", sha256: "h" },
      prompt_template: { blob_ref: "new-file", mode: "inline_content", sha256: "old-hash" },
      queries: [{ name: "gone", template: { blob_ref: "deleted", mode: "inline_content", sha256: "x" } }, { name: "typed", template: "Typed text." }],
    } }, { id: "empty", plugin: "llm", options: { prompt_template: "Inline." } }] } as unknown as CompositionState;
    render(<FileWiringPad blob={blob} state={current} blobs={[blob, other]} disabledReason={null} onConnect={vi.fn()} onClose={vi.fn()} />);
    const llm = within(step("llm"));
    expect(llm.getByRole("checkbox", { name: /System prompt/ })).toHaveAccessibleName(/Replaces rubric\.txt/);
    expect(llm.getByRole("checkbox", { name: /User prompt, query gone/ })).toHaveAccessibleName(/Replaces a file that is no longer in this session/);
    expect(llm.getByRole("checkbox", { name: /User prompt, query typed/ })).toHaveAccessibleName(/Replaces the inline prompt/);
    expect(within(step("empty")).getByRole("checkbox", { name: /System prompt/ })).toHaveAccessibleName(/Not connected/);
  });

  it("groups prompts by step and filters by step or prompt name, keeping hidden selections", async () => {
    const { onConnect, user } = setup();
    expect(screen.getAllByRole("group").map((group) => group.querySelector("legend")?.textContent))
      .toEqual(["How to connect", "Prompt connections", "single", "multi", "mapping"]);
    await user.click(within(step("single")).getByRole("checkbox", { name: /User prompt/ }));
    const filter = screen.getByRole("searchbox", { name: "Filter by step or prompt" });
    await user.type(filter, "query");
    expect(screen.queryByRole("group", { name: "single" })).not.toBeInTheDocument();
    // The step's system prompt does not match "query"; its two queries do.
    expect(within(step("multi")).getAllByRole("checkbox")).toHaveLength(2);
    await user.clear(filter);
    await user.type(filter, "MAPP");
    expect(screen.getAllByRole("group").map((group) => group.querySelector("legend")?.textContent)).toEqual(["How to connect", "Prompt connections", "mapping"]);
    await user.clear(filter);
    await user.type(filter, "nothing here");
    expect(screen.getByText("No step or prompt matches this filter.")).toBeInTheDocument();
    // A selection the filter hides still counts and is still sent.
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    expect(onConnect.mock.calls[0][0]).toContain("node:single.options.prompt_template");
  });

  it("announces a refusal in the footer, and a repeat as a new announcement", () => {
    const { rerender } = render(<FileWiringPad blob={blob} state={state} blobs={[blob]} disabledReason={null}
      sendRefusal={{ message: "Composer did not take this request.", seq: 1 }} onConnect={vi.fn()} onClose={vi.fn()} />);
    const status = screen.getByRole("status");
    expect(status.closest("footer")).not.toBeNull();
    const first = status.firstElementChild;
    expect(first).toHaveTextContent("Composer did not take this request.");
    rerender(<FileWiringPad blob={blob} state={state} blobs={[blob]} disabledReason={null}
      sendRefusal={{ message: "Composer did not take this request.", seq: 2 }} onConnect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("status")).toBe(status);
    expect(status.firstElementChild).not.toBe(first);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("closes on Escape without requesting a connection", async () => {
    const { onConnect, onClose, user } = setup();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    expect(onConnect).not.toHaveBeenCalled();
  });
});
