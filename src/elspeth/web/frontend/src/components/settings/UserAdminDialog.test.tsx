import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserAdminDialog } from "./UserAdminDialog";
import * as api from "@/api/client";

vi.mock("@/api/client", () => ({
  fetchAdminUsers: vi.fn(),
  createAdminUser: vi.fn(),
  resetAdminUserPassword: vi.fn(),
  deleteAdminUser: vi.fn(),
}));

const fetchAdminUsers = vi.mocked(api.fetchAdminUsers);
const createAdminUser = vi.mocked(api.createAdminUser);
const resetAdminUserPassword = vi.mocked(api.resetAdminUserPassword);
const deleteAdminUser = vi.mocked(api.deleteAdminUser);

const TWO_USERS = {
  users: [
    {
      user_id: "john",
      display_name: "John",
      email: null,
      email_verified: true,
    },
    {
      user_id: "alice",
      display_name: "Alice",
      email: "alice@example.com",
      email_verified: true,
    },
  ],
};

describe("UserAdminDialog", () => {
  const onClose = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    fetchAdminUsers.mockResolvedValue(TWO_USERS);
  });

  it("lists accounts on open", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("John")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
  });

  it("mounts the modal chrome on the app-dialog primitive (elspeth-e6fcd8d703)", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    // The frame was a copy-pasted inline style object at literal z-index 101
    // (the non-modal overlay band) with a string box-shadow no theme override
    // could reach. It now composes the shared classes — the CSS side is gated
    // in styles/overlayChrome.test.ts; this pins that the markup actually
    // reaches those rules. The width/type-scale closure is the wide settings
    // variant.
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("style")).toBeNull();
    expect(dialog).toHaveClass(
      "app-dialog",
      "settings-dialog",
      "settings-dialog-wide",
    );
    expect(screen.getByRole("presentation")).toHaveClass("app-dialog-backdrop");
    expect(
      screen.getByRole("button", { name: "Close user management dialog" }),
    ).toHaveClass("dialog-close");
  });

  it("offers no Delete button on the signed-in admin's own row", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");
    // One row (alice) has Delete; john's row must not.
    expect(screen.getAllByRole("button", { name: /^delete$/i })).toHaveLength(
      1,
    );
  });

  it("creates a user and shows the one-time password with copy", async () => {
    createAdminUser.mockResolvedValue({ user_id: "bob", password: "s3cretpw" });
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    await userEvent.type(screen.getByLabelText(/username/i), "bob");
    await userEvent.type(screen.getByLabelText(/display name/i), "Bob");
    await userEvent.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() =>
      expect(createAdminUser).toHaveBeenCalledWith({
        username: "bob",
        display_name: "Bob",
      }),
    );
    expect(await screen.findByTestId("generated-password")).toHaveTextContent(
      "s3cretpw",
    );
    // The list reloads after a successful mutation.
    expect(fetchAdminUsers).toHaveBeenCalledTimes(2);
  });

  it("resets a password and shows the one-time password", async () => {
    resetAdminUserPassword.mockResolvedValue({
      user_id: "alice",
      password: "n3wpw",
    });
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    await userEvent.click(
      screen.getAllByRole("button", { name: /reset password/i })[1],
    );

    await waitFor(() =>
      expect(resetAdminUserPassword).toHaveBeenCalledWith("alice"),
    );
    expect(await screen.findByTestId("generated-password")).toHaveTextContent(
      "n3wpw",
    );
  });

  it("requires a second click to delete", async () => {
    deleteAdminUser.mockResolvedValue(undefined);
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    await userEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(deleteAdminUser).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole("button", { name: /confirm delete/i }),
    );
    await waitFor(() => expect(deleteAdminUser).toHaveBeenCalledWith("alice"));
  });

  it("holds the delete control's width across the confirm step (elspeth-a0700fefff)", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    const deleteBtn = screen.getByRole("button", { name: /^delete$/i });
    const slot = deleteBtn.parentElement as HTMLElement;
    expect(slot).toHaveClass("user-admin-delete-slot");

    const sizer = slot.querySelector<HTMLElement>(".user-admin-delete-sizer");
    expect(sizer).not.toBeNull();
    // Hidden from AT and from the pointer — it exists only to reserve width.
    expect(sizer).toHaveAttribute("aria-hidden", "true");
    // It reserves the LONGEST label the control can take, so the cell (which
    // is right-aligned and nowrap) cannot drag both buttons left out from
    // under the pointer when "Delete" becomes "Confirm delete".
    expect(sizer).toHaveTextContent("Confirm delete");
    // ...at the live control's own metrics, so padding/font/border can never
    // drift apart from the button it is sizing.
    for (const cls of deleteBtn.classList) {
      expect(sizer).toHaveClass(cls);
    }

    await userEvent.click(deleteBtn);
    const confirmBtn = screen.getByRole("button", { name: /confirm delete/i });
    expect(confirmBtn.parentElement).toBe(slot);
    expect(
      slot.querySelector<HTMLElement>(".user-admin-delete-sizer"),
    ).toHaveTextContent("Confirm delete");
  });

  it("separates the row actions with a styled gap, not a literal text space", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    const resetBtn = screen.getAllByRole("button", {
      name: /reset password/i,
    })[1];
    const group = resetBtn.parentElement as HTMLElement;
    expect(group).toHaveClass("user-admin-row-action-group");
    // The old gap was a literal {" "} text node (~3.4px, less than half the
    // --space-sm the password row uses). Any bare text node between the
    // actions means the gap is back to being typography.
    const textNodes = Array.from(group.childNodes).filter(
      (node) => node.nodeType === node.TEXT_NODE,
    );
    expect(textNodes).toHaveLength(0);
  });

  it("surfaces API failures in an alert region", async () => {
    resetAdminUserPassword.mockRejectedValue(new Error("User not found"));
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    await userEvent.click(
      screen.getAllByRole("button", { name: /reset password/i })[0],
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "User not found",
    );
  });

  it("Escape closes the dialog", async () => {
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("moves focus to the password banner after a create", async () => {
    createAdminUser.mockResolvedValue({ user_id: "bob", password: "s3cretpw" });
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");

    await userEvent.type(screen.getByLabelText(/username/i), "bob");
    await userEvent.type(screen.getByLabelText(/display name/i), "Bob");
    await userEvent.click(screen.getByRole("button", { name: /create/i }));

    const banner = await screen.findByRole("status");
    await waitFor(() => expect(document.activeElement).toBe(banner));
  });

  it("announces a clipboard failure instead of failing silently", async () => {
    resetAdminUserPassword.mockResolvedValue({
      user_id: "alice",
      password: "n3wpw",
    });
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    await screen.findByText("Alice");
    await userEvent.click(
      screen.getAllByRole("button", { name: /reset password/i })[1],
    );
    await screen.findByTestId("generated-password");

    // jsdom ships no navigator.clipboard; install a failing one.
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    await userEvent.click(screen.getByRole("button", { name: /copy/i }));

    expect(await screen.findByText(/copy failed/i)).toBeInTheDocument();
  });

  it("shows an empty state when no accounts exist", async () => {
    fetchAdminUsers.mockResolvedValue({ users: [] });
    render(<UserAdminDialog onClose={onClose} currentUserId="john" />);
    expect(await screen.findByText(/no accounts yet/i)).toBeInTheDocument();
  });
});
