import { beforeEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserMenu } from "./UserMenu";
import { useAuthStore } from "@/stores/authStore";
import type { UserProfile } from "@/types/index";

/** Seedable /api/auth/me profile for the identity-header tests. */
function makeUser(overrides: Partial<UserProfile> = {}): UserProfile {
  return {
    user_id: "user-1",
    username: "jdoe",
    display_name: "Jane Doe",
    email: null,
    groups: [],
    dev_admin: false,
    ...overrides,
  };
}

/** The identity row's announced text, visually-hidden prefixes included —
 *  the row carries no role or accessible name of its own, so its normalized
 *  textContent IS what a screen reader reads out. */
function identityText(element: Element | null): string {
  return (element?.textContent ?? "").replace(/\s+/g, " ").trim();
}

// Role contract: this component is a disclosure/popover, NOT a WAI-ARIA
// `menu` widget. Tests query items by their implicit `button` role rather
// than `menuitem` — the menu role was dropped because we don't implement
// the arrow-key/Home/End/type-ahead keyboard contract that the menu
// pattern demands. See UserMenu.tsx module comment.
describe("UserMenu", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.colorScheme = "";
    // Signed-out by default; identity-header tests seed their own user.
    useAuthStore.setState({ user: null });
  });

  it("is closed by default — action buttons not in the document", () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    expect(
      screen.queryByRole("button", { name: /composer preferences/i }),
    ).not.toBeInTheDocument();
  });

  it("shows theme, Composer preferences, and Sign out items when opened", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(
      screen.getByRole("button", { name: /switch to light theme/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /composer preferences/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();
  });

  // elspeth-66257bfab1: the theme row used to lead with U+2600 / U+263E, the
  // only glyph in a five-row text menu, drawn from the system symbol font
  // rather than the product icon set. The guard is written over the whole
  // rendered menu, not over one span, so re-introducing a pictograph anywhere
  // in the menu — or on either theme — reddens it.
  it("carries no decorative pictograph on any row, in either theme", async () => {
    for (const startingTheme of ["dark", "light"] as const) {
      localStorage.setItem("elspeth_theme", startingTheme);
      const { unmount } = render(
        <UserMenu
          onOpenSettings={vi.fn()}
          onSignOut={vi.fn()}
          onOpenUserManagement={vi.fn()}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: /account/i }));

      const list = screen.getByRole("list");
      // Sanity: the theme row really is showing, so this is not vacuous.
      expect(
        screen.getByRole("button", { name: /switch to (light|dark) theme/i }),
      ).toBeInTheDocument();
      // Symbol/emoji blocks: Miscellaneous Symbols (U+2600-26FF), Dingbats
      // (U+2700-27BF), Misc Symbols & Pictographs, Emoticons, Transport,
      // and Supplemental Symbols & Pictographs.
      const pictographs =
        /[☀-➿\u{1F300}-\u{1F5FF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}]/u;
      // textContent includes the text of aria-hidden spans, so a decorative
      // glyph is caught however it is marked up. Deliberately NOT asserting
      // "no aria-hidden elements": elspeth-66257bfab1's own second remedy is
      // an <Icon name="sun"/> from the product icon set, which is aria-hidden
      // by construction and would be a legitimate future implementation.
      expect(list.textContent ?? "").not.toMatch(pictographs);

      unmount();
      localStorage.clear();
      document.documentElement.removeAttribute("data-theme");
    }
  });

  it("calls onOpenSettings when Composer preferences is clicked, then closes", async () => {
    const openSettings = vi.fn();
    render(<UserMenu onOpenSettings={openSettings} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(
      screen.getByRole("button", { name: /composer preferences/i }),
    );
    expect(openSettings).toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /composer preferences/i }),
    ).not.toBeInTheDocument();
  });

  // elspeth-bcd1a9b9b3: the dialog that onOpenSettings opens restores focus to
  // whatever document.activeElement was when it mounted. Closing the menu first
  // detaches the clicked item, browsers reset focus to <body>, and the dialog
  // then captures <body> as its restore target — so on close the keyboard user
  // lands nowhere. Handing focus back to the trigger BEFORE the item unmounts
  // gives the dialog a live element to restore to.
  it("returns focus to the Account trigger when Composer preferences is chosen", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: /account/i });
    await userEvent.click(trigger);
    await userEvent.click(
      screen.getByRole("button", { name: /composer preferences/i }),
    );
    expect(document.activeElement).toBe(trigger);
  });

  it("toggles the theme from the account menu", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    await userEvent.click(
      screen.getByRole("button", { name: /switch to light theme/i }),
    );

    expect(localStorage.getItem("elspeth_theme")).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(
      screen.queryByRole("button", { name: /switch to dark theme/i }),
    ).not.toBeInTheDocument();
  });

  it("calls onSignOut when Sign out is clicked", async () => {
    const signOut = vi.fn();
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={signOut} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));
    expect(signOut).toHaveBeenCalled();
  });

  it("closes when clicking outside the menu", async () => {
    render(
      <div>
        <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />
        <button type="button">outside</button>
      </div>,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(
      screen.getByRole("button", { name: /composer preferences/i }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /outside/i }));
    expect(
      screen.queryByRole("button", { name: /composer preferences/i }),
    ).not.toBeInTheDocument();
  });

  it("Escape closes the menu and returns focus to the trigger", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: /account/i });
    await userEvent.click(trigger);
    expect(
      screen.getByRole("button", { name: /composer preferences/i }),
    ).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(
      screen.queryByRole("button", { name: /composer preferences/i }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("Tab navigates between action buttons (project convention: Tab not arrows)", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: /switch to light theme/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: /composer preferences/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(
      screen.getByRole("link", { name: /help & documentation/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toHaveFocus();
  });

  // elspeth-8225736807: one honest help entry — a link to the repository
  // docs directory (the deployment serves no docs site of its own).
  it("offers a 'Help & documentation' link to the project docs", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    const help = screen.getByRole("link", { name: /help & documentation/i });
    expect(help).toHaveAttribute(
      "href",
      "https://github.com/johnm-dta/elspeth/tree/main/docs",
    );
    // New tab, no opener leakage.
    expect(help).toHaveAttribute("target", "_blank");
    expect(help).toHaveAttribute("rel", "noreferrer");
  });

  // elspeth-83eb51334f: focus leaving the menu subtree closes it — a
  // keyboard user must not be able to Tab away while the popup stays open.
  it("closes when focus moves outside the menu subtree", async () => {
    render(
      <div>
        <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />
        <button type="button">outside</button>
      </div>,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    const signOut = screen.getByRole("button", { name: /sign out/i });
    signOut.focus();
    const outside = screen.getByRole("button", { name: /^outside$/i });
    fireEvent.blur(signOut, { relatedTarget: outside });
    expect(
      screen.queryByRole("button", { name: /sign out/i }),
    ).not.toBeInTheDocument();
  });

  it("stays open when focus moves between items inside the menu", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    const theme = screen.getByRole("button", { name: /switch to/i });
    const signOut = screen.getByRole("button", { name: /sign out/i });
    theme.focus();
    fireEvent.blur(theme, { relatedTarget: signOut });
    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();
  });

  it("trigger advertises aria-haspopup=true (disclosure, not menu)", () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    // Regression pin for the role-contract fix: the trigger MUST NOT
    // assert aria-haspopup="menu" because the component doesn't honour
    // the WAI-ARIA menu keyboard contract (arrow keys, Home/End,
    // type-ahead). "true" is the no-promise-of-specific-popup-role
    // value the disclosure pattern uses.
    const trigger = screen.getByRole("button", { name: /account/i });
    expect(trigger).toHaveAttribute("aria-haspopup", "true");
  });
});

describe("UserMenu dev-admin entry", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.colorScheme = "";
    // Signed-out by default; identity-header tests seed their own user.
    useAuthStore.setState({ user: null });
  });

  it("hides User management when onOpenUserManagement is absent", async () => {
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(
      screen.queryByRole("button", { name: /user management/i }),
    ).not.toBeInTheDocument();
  });

  it("shows User management and invokes the callback, then closes", async () => {
    const openUserManagement = vi.fn();
    render(
      <UserMenu
        onOpenSettings={vi.fn()}
        onSignOut={vi.fn()}
        onOpenUserManagement={openUserManagement}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(
      screen.getByRole("button", { name: /user management/i }),
    );
    expect(openUserManagement).toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /user management/i }),
    ).not.toBeInTheDocument();
  });

  it("returns focus to the Account trigger when User management is chosen", async () => {
    render(
      <UserMenu
        onOpenSettings={vi.fn()}
        onSignOut={vi.fn()}
        onOpenUserManagement={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("button", { name: /account/i });
    await userEvent.click(trigger);
    await userEvent.click(
      screen.getByRole("button", { name: /user management/i }),
    );
    expect(document.activeElement).toBe(trigger);
  });
});

// elspeth-312238838a: the dropdown says who is signed in. The identity row is
// a plain non-focusable <li> — never a button/link — so the pinned Tab order
// across the action items is untouched.
describe("UserMenu identity header", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.colorScheme = "";
    useAuthStore.setState({ user: null });
  });

  it("renders display name + username first in the list, not Tab-reachable", async () => {
    useAuthStore.setState({ user: makeUser() });
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    const identity = screen.getByText("Jane Doe").closest("li");
    expect(identity).not.toBeNull();
    expect(identity).toHaveClass("user-menu-identity");
    // The ACCESSIBLE text, not just "the name renders": the visually-hidden
    // prefixes are what state whose account this is and what the second
    // line means, so they are part of the contract.
    expect(identityText(identity)).toBe("Signed in as Jane Doe username: jdoe");
    const list = screen.getByRole("list");
    expect(list.firstElementChild).toBe(identity);
    // Non-interactive: no button/link inside the identity row.
    expect(identity?.querySelector("button, a")).toBeNull();
    // Not reachable by Tab: the first Tab stop after the trigger is the
    // theme action, and the full pinned four-action sequence still holds.
    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: /switch to light theme/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(
      screen.getByRole("button", { name: /composer preferences/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(
      screen.getByRole("link", { name: /help & documentation/i }),
    ).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: /sign out/i })).toHaveFocus();
  });

  it("renders the username as the primary line when display_name is null", async () => {
    useAuthStore.setState({ user: makeUser({ display_name: null }) });
    const { container } = render(
      <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    const primary = container.querySelector(".user-menu-identity-name");
    expect(primary).toHaveTextContent("jdoe");
    // No secondary line — the username is not repeated.
    expect(container.querySelector(".user-menu-identity-username")).toBeNull();
    expect(identityText(container.querySelector(".user-menu-identity"))).toBe(
      "Signed in as jdoe",
    );
  });

  // Live for local auth: UserIdentity is built with username == user_id and
  // display_name is a separate registration field that users and harnesses
  // routinely set to the same string. The second line must appear only when
  // it carries information the first does not.
  it("does not repeat the identity when display_name equals the username", async () => {
    useAuthStore.setState({
      user: makeUser({ username: "jsmith", display_name: "jsmith" }),
    });
    const { container } = render(
      <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    const identity = container.querySelector(".user-menu-identity");
    expect(identityText(identity)).toBe("Signed in as jsmith");
    expect(container.querySelector(".user-menu-identity-username")).toBeNull();
    expect(screen.getAllByText("jsmith")).toHaveLength(1);
  });

  // Surrounding whitespace is not part of a name — and trimming is what
  // makes the blank case below absence rather than a name made of spaces.
  it("trims surrounding whitespace from the display name", async () => {
    useAuthStore.setState({
      user: makeUser({ username: "jdoe", display_name: "  Jane Doe  " }),
    });
    const { container } = render(
      <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    expect(
      container.querySelector(".user-menu-identity-name")?.textContent,
    ).toBe("Jane Doe");
  });

  // An empty-string display_name is absence, not a name: rendering it left a
  // blank primary line above a muted username.
  it("falls back to the username when display_name is blank", async () => {
    useAuthStore.setState({
      user: makeUser({ username: "jdoe", display_name: "   " }),
    });
    const { container } = render(
      <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));

    const primary = container.querySelector(".user-menu-identity-name");
    expect(primary).toHaveTextContent("jdoe");
    expect(identityText(container.querySelector(".user-menu-identity"))).toBe(
      "Signed in as jdoe",
    );
    expect(container.querySelector(".user-menu-identity-username")).toBeNull();
  });

  it("renders no identity block when no user is signed in", async () => {
    const { container } = render(
      <UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(container.querySelector(".user-menu-identity")).toBeNull();
    // Action items are unaffected.
    expect(
      screen.getByRole("button", { name: /sign out/i }),
    ).toBeInTheDocument();
  });
});
