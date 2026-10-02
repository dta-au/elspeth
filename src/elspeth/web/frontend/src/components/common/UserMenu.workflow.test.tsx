import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";
import { UserMenu } from "./UserMenu";

const user = { user_id: "alice", username: "alice", display_name: null, email: null, groups: [], dev_admin: false };
const summary = { governance: "on" as const, roles: ["admin" as const], approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 };

describe("workflow account entries", () => {
  afterEach(() => {
    useAuthStore.setState({ token: null, user: null });
    useMailboxStore.getState().reset();
  });

  it("opens mailbox, library and People & access with focus returned to Account", async () => {
    useAuthStore.setState({ token: "test-token", user });
    useMailboxStore.setState({ summary });
    const onOpenMailbox = vi.fn();
    const onOpenLibrary = vi.fn();
    const onOpenPeopleAccess = vi.fn();
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={onOpenMailbox} onOpenLibrary={onOpenLibrary} onOpenPeopleAccess={onOpenPeopleAccess} />);
    const trigger = screen.getByRole("button", { name: "account menu" });
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "Mailbox" }));
    expect(onOpenMailbox).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "Shared library" }));
    expect(onOpenLibrary).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "People & access" }));
    expect(onOpenPeopleAccess).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
  });

  it("never infers People & access from the mailbox summary's roles", async () => {
    // The mailbox says "admin", but the App has not offered the entry. The
    // menu follows the App's capability decision and nothing else: a mailbox
    // that is slow, stale or failed must not decide whether administration exists.
    useAuthStore.setState({ token: "test-token", user });
    useMailboxStore.setState({ summary });
    const { rerender } = render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "account menu" }));
    expect(screen.queryByRole("button", { name: "People & access" })).not.toBeInTheDocument();
    act(() => useMailboxStore.setState({ summary: { ...summary, roles: [] } }));
    rerender(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={vi.fn()} onOpenPeopleAccess={vi.fn()} />);
    expect(screen.getByRole("button", { name: "People & access" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mailbox" })).toBeInTheDocument();
  });
});
