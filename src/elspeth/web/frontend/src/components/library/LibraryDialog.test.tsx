import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as library from "@/api/library";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { LibraryEntry } from "@/types/library";
import { LibraryDialog } from "./LibraryDialog";

vi.mock("@/api/library", () => ({
  fetchLibrary: vi.fn(), publishLibraryEntry: vi.fn(), curateLibraryEntry: vi.fn(), forkLibraryEntry: vi.fn(),
}));

const api = vi.mocked(library);
const loadSessions = vi.fn().mockResolvedValue(undefined);
const selectSession = vi.fn().mockResolvedValue(undefined);
const refreshSummary = vi.fn().mockResolvedValue(undefined);

function entry(overrides: Partial<LibraryEntry> = {}): LibraryEntry {
  return {
    entry_id: "entry-1", published_from_session_id: null, payload_digest: "a".repeat(64),
    compartment_id: "compartment-a", title: "Invoice triage", version: 2,
    published_by_identity_id: "author", curated_by_identity_id: "curator",
    published_at: "2026-09-19T00:00:00Z", accepted_at: "2026-09-19T01:00:00Z",
    rejected_at: null, rejection_note: null, deprecated_at: null, recalled_at: null,
    note: null, state: "accepted", ...overrides,
  };
}

function roles(...values: ("user" | "curator")[]): void {
  useMailboxStore.setState({
    summary: { governance: "on", roles: values, approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 },
    refreshSummary,
  });
}

describe("LibraryDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    roles();
    useAuthStore.setState({ user: { user_id: "viewer" } as never });
    useSessionStore.setState({ activeSessionId: null, compositionState: null, loadSessions, selectSession });
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [] });
  });

  it("shows accepted projections with origin marking and labels deprecated entries", async () => {
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [entry(), entry({
      entry_id: "older", title: "Older triage", state: "deprecated", deprecated_at: "2026-09-19T02:00:00Z",
    })] });
    render(<LibraryDialog onClose={vi.fn()} />);
    expect(await screen.findByText("Invoice triage")).toBeInTheDocument();
    expect(screen.getAllByText("Version 2 · Origin compartment: compartment-a")).toHaveLength(2);
    expect(screen.getAllByText(`Content digest: ${"a".repeat(64)}`)).toHaveLength(2);
    expect(screen.getByText("Deprecated")).toBeInTheDocument();
    expect(api.fetchLibrary).toHaveBeenCalledWith("accepted");
    expect(screen.queryByRole("tab", { name: "Curator queue" })).toBeNull();
  });

  it("forks an accepted entry and opens the new session", async () => {
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [entry()] });
    api.forkLibraryEntry.mockResolvedValue({ session_id: "new-session", state_id: "new-state" });
    const onClose = vi.fn();
    render(<LibraryDialog onClose={onClose} />);
    await userEvent.click(await screen.findByRole("button", { name: "Fork Invoice triage" }));
    await waitFor(() => expect(api.forkLibraryEntry).toHaveBeenCalledWith("entry-1"));
    await waitFor(() => expect(selectSession).toHaveBeenCalledWith("new-session"));
    expect(loadSessions).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not open a recalled entry after a named fork conflict", async () => {
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [entry()] });
    api.forkLibraryEntry.mockRejectedValue({ status: 409, error_type: "library_entry_not_forkable", detail: "Library entry was recalled", current_state: "recalled" });
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Fork Invoice triage" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Current state: recalled");
    expect(screen.queryByRole("button", { name: "Fork Invoice triage" })).toBeNull();
    expect(selectSession).not.toHaveBeenCalled();
  });

  it("treats governance off as a deployment state", async () => {
    api.fetchLibrary.mockRejectedValue({ status: 409, error_type: "workflow_governance_off", detail: "off" });
    render(<LibraryDialog onClose={vi.fn()} />);
    expect(await screen.findByText("The shared library is not enabled on this deployment.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("publishes the current state and opens My publications", async () => {
    roles("user");
    useSessionStore.setState({ activeSessionId: "session-current", compositionState: {} as never });
    api.publishLibraryEntry.mockResolvedValue(entry({ state: "pending", accepted_at: null }));
    api.fetchLibrary.mockImplementation(async (view) => ({ view, entries: view === "mine" ? [entry({ state: "pending", accepted_at: null })] : [] }));
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Title"), "My pipeline");
    await userEvent.click(screen.getByRole("button", { name: "Publish for review" }));
    await waitFor(() => expect(api.publishLibraryEntry).toHaveBeenCalledWith("session-current", "My pipeline"));
    expect(await screen.findByRole("tab", { name: "My publications" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Status: pending")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Published for curator review");
  });

  it("names a profile-bound source when publication is refused", async () => {
    roles("user");
    useSessionStore.setState({ activeSessionId: "session-current", compositionState: {} as never });
    api.publishLibraryEntry.mockRejectedValue({
      status: 409, error_type: "library_entry_needs_profile_bound_source",
      detail: "Publish a profile-bound source instead", sources: ["invoices"],
    });
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Title"), "Invoice triage");
    await userEvent.click(screen.getByRole("button", { name: "Publish for review" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Affected sources: invoices");
    expect(screen.getByRole("tab", { name: "Browse" })).toHaveAttribute("aria-selected", "true");
  });

  it("requires a reason for rejection and prevents self-curation", async () => {
    roles("curator");
    api.fetchLibrary.mockImplementation(async (view) => ({ view, entries: view === "queue" ? [
      entry({ state: "pending", accepted_at: null }),
      entry({ entry_id: "own", title: "Own entry", state: "pending", accepted_at: null, published_by_identity_id: "viewer" }),
    ] : [] }));
    api.curateLibraryEntry.mockResolvedValue(entry({ state: "rejected", accepted_at: null, rejected_at: "2026-09-19T03:00:00Z" }));
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Curator queue" }));
    const panel = await screen.findByRole("tabpanel", { name: "Curator queue" });
    expect(within(panel).queryByRole("button", { name: "Accept Own entry" })).toBeNull();
    await userEvent.click(within(panel).getByRole("button", { name: "Reject Invoice triage" }));
    expect(screen.getByRole("button", { name: "Confirm reject" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason (required)"), "Bad source mapping");
    await userEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() => expect(api.curateLibraryEntry).toHaveBeenCalledWith("entry-1", "reject", "Bad source mapping"));
  });

  it("shows a curator state race and reloads the queue", async () => {
    roles("curator");
    api.fetchLibrary.mockImplementation(async (view) => ({ view, entries: view === "queue" ? [entry({ state: "pending", accepted_at: null })] : [] }));
    api.curateLibraryEntry.mockRejectedValue({ status: 409, error_type: "library_entry_already_curated", detail: "Entry was already curated", current_state: "rejected" });
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Curator queue" }));
    await userEvent.click(await screen.findByRole("button", { name: "Accept Invoice triage" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm accept" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Current state: rejected");
    await waitFor(() => expect(api.fetchLibrary).toHaveBeenCalledWith("queue"));
    expect(api.fetchLibrary.mock.calls.filter(([view]) => view === "queue")).toHaveLength(2);
  });

  it("keeps an overlong UTF-8 curator note out of the request", async () => {
    roles("curator");
    api.fetchLibrary.mockImplementation(async (view) => ({ view, entries: view === "queue" ? [entry({ state: "pending", accepted_at: null })] : [] }));
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Curator queue" }));
    await userEvent.click(await screen.findByRole("button", { name: "Reject Invoice triage" }));
    const field = screen.getByLabelText("Reason (required)");
    fireEvent.change(field, { target: { value: "é".repeat(2050) } });
    expect(screen.getByRole("button", { name: "Confirm reject" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("4 KiB limit");
    expect(api.curateLibraryEntry).not.toHaveBeenCalled();
  });
});
