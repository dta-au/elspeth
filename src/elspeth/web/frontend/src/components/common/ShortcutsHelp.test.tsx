import { readFileSync } from "node:fs";
import { useState } from "react";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ShortcutsHelp } from "./ShortcutsHelp";

// Phase 8c-5: Four-group reorganisation tests.
//
// The component renders four <section aria-label="…"> elements containing
// <h3> headings and <dl> shortcut lists. Tests below verify:
//   1. All four groups are rendered.
//   2. Each shortcut is associated with exactly one group (kbd → closest section).
//   3. Obsolete Alt+1-4 inspector-tab shortcuts are absent.
//   4. All live shortcuts appear in the correct group.

const GROUP_NAMES = ["Actions", "Navigation", "Reference", "Editing"] as const;

describe("ShortcutsHelp — four-group structure", () => {
  it("uses a unique labelled title and a bounded header/body/footer structure", () => {
    render(
      <>
        <ShortcutsHelp onClose={vi.fn()} />
        <ShortcutsHelp onClose={vi.fn()} />
      </>,
    );

    const dialogs = screen.getAllByRole("dialog", {
      name: "Keyboard Shortcuts",
    });
    expect(dialogs).toHaveLength(2);
    const titleIds = dialogs.map((dialog) =>
      dialog.getAttribute("aria-labelledby"),
    );
    expect(new Set(titleIds).size).toBe(2);
    for (const [index, dialog] of dialogs.entries()) {
      expect(dialog).not.toHaveAttribute("aria-label");
      expect(within(dialog).getByRole("heading", { level: 2 })).toHaveAttribute(
        "id",
        titleIds[index],
      );
      expect(dialog.querySelectorAll(":scope > .confirm-dialog-header"))
        .toHaveLength(1);
      expect(dialog.querySelectorAll(":scope > .confirm-dialog-body"))
        .toHaveLength(1);
      expect(dialog.querySelectorAll(":scope > .confirm-dialog-actions"))
        .toHaveLength(1);
    }

    const css = readFileSync("src/styles/shared.css", "utf8");
    expect(css).toMatch(
      /\.confirm-dialog-body\s*\{[^}]*overflow-y:\s*auto;/s,
    );
  });

  it("restores focus to the exact invoker after Close unmounts the dialog", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open shortcuts
          </button>
          {open ? <ShortcutsHelp onClose={() => setOpen(false)} /> : null}
        </>
      );
    }

    render(<Harness />);
    const invoker = screen.getByRole("button", { name: "Open shortcuts" });
    await user.click(invoker);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(invoker).toHaveFocus();
  });

  it("renders an Actions group", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    expect(
      screen.getByRole("heading", { name: /^actions$/i }),
    ).toBeInTheDocument();
  });

  it("renders a Reference group", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    expect(
      screen.getByRole("heading", { name: /^reference$/i }),
    ).toBeInTheDocument();
  });

  it("renders a Navigation group", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    expect(
      screen.getByRole("heading", { name: /^navigation$/i }),
    ).toBeInTheDocument();
  });

  it("renders an Editing group", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    expect(
      screen.getByRole("heading", { name: /^editing$/i }),
    ).toBeInTheDocument();
  });

  it("each shortcut kbd is associated with exactly one group", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const kbdEls = document.querySelectorAll("kbd");
    expect(kbdEls.length).toBeGreaterThan(0);
    for (const kbd of kbdEls) {
      const section = kbd.closest("section");
      expect(section).not.toBeNull();
      const label = section?.getAttribute("aria-label") ?? "";
      expect(GROUP_NAMES).toContain(label);
    }
  });

  it("the obsolete Alt+1-4 inspector-tab shortcuts are absent", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    expect(document.body.textContent).not.toMatch(/Alt\+1-4/);
    expect(document.body.textContent).not.toMatch(/Spec\/Graph\/YAML\/Runs/);
    // Phase-3 removed the inspector tabs; Alt+1/2/3/4 were never added to
    // App.tsx (SWITCH_TAB_EVENT has no subscriber). They must not appear here.
    expect(document.body.textContent).not.toMatch(/Switch inspector tab/i);
  });

  it("places 'Ctrl+N New session' and 'Ctrl+E Run pipeline' in Actions", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const actionsSection = document
      .querySelector("section[aria-label='Actions']");
    expect(actionsSection).not.toBeNull();
    expect(actionsSection?.textContent).toMatch(/new session/i);
    expect(actionsSection?.textContent).toMatch(/run pipeline/i);
  });

  it("places 'Ctrl+K Command palette' and 'Ctrl+/ Focus chat input' in Navigation", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const navSection = document
      .querySelector("section[aria-label='Navigation']");
    expect(navSection).not.toBeNull();
    expect(navSection?.textContent).toMatch(/command palette/i);
    expect(navSection?.textContent).toMatch(/focus chat input/i);
  });

  it("places 'Open plugin catalog' and '?' Keyboard shortcuts in Reference", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const refSection = document
      .querySelector("section[aria-label='Reference']");
    expect(refSection).not.toBeNull();
    expect(refSection?.textContent).toMatch(/open plugin catalog/i);
    expect(refSection?.textContent).toMatch(/keyboard shortcuts/i);
  });

  it("places 'Escape' in Editing", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const editSection = document
      .querySelector("section[aria-label='Editing']");
    expect(editSection).not.toBeNull();
    expect(editSection?.textContent).toMatch(/close dialog or drawer/i);
  });

  it("places 'Ctrl+Shift+V Validate pipeline' in Actions (requestValidate has live consumers)", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const actionsSection = document
      .querySelector("section[aria-label='Actions']");
    expect(actionsSection?.textContent).toMatch(/validate pipeline/i);
  });

  it("places persistent Graph and YAML artifact shortcuts in Navigation", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const actionsSection = document.querySelector(
      "section[aria-label='Actions']",
    );
    const navSection = document.querySelector(
      "section[aria-label='Navigation']",
    );
    expect(actionsSection?.textContent).not.toMatch(/graph|yaml/i);
    expect(navSection?.textContent).toMatch(/show graph/i);
    expect(navSection?.textContent).toMatch(/show yaml/i);
  });

  it("places 'Alt+1-3 Switch catalog tab' in Navigation", () => {
    render(<ShortcutsHelp onClose={vi.fn()} />);
    const navSection = document
      .querySelector("section[aria-label='Navigation']");
    expect(navSection?.textContent).toMatch(/alt\+1-3/i);
    expect(navSection?.textContent).toMatch(/switch catalog tab/i);
  });
});
