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

  it("opens mailbox, library and live-role admin with focus returned to Account", async () => {
    useAuthStore.setState({ token: "test-token", user });
    useMailboxStore.setState({ summary });
    const onOpenMailbox = vi.fn();
    const onOpenLibrary = vi.fn();
    const onOpenIdentityAdmin = vi.fn();
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={onOpenMailbox} onOpenLibrary={onOpenLibrary} onOpenIdentityAdmin={onOpenIdentityAdmin} />);
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
    await userEvent.click(screen.getByRole("button", { name: "Identity administration" }));
    expect(onOpenIdentityAdmin).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
  });

  it("hides admin as soon as its live role leaves the summary", async () => {
    useAuthStore.setState({ token: "test-token", user });
    useMailboxStore.setState({ summary });
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={vi.fn()} onOpenIdentityAdmin={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "account menu" }));
    expect(screen.getByRole("button", { name: "Identity administration" })).toBeInTheDocument();
    act(() => useMailboxStore.setState({ summary: { ...summary, roles: [] } }));
    expect(screen.queryByRole("button", { name: "Identity administration" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mailbox" })).toBeInTheDocument();
  });
});
