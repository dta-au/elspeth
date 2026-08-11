import { readFileSync } from "node:fs";
import { useState } from "react";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";

function Dialog(props: { title: string; message: string; onCancel?: () => void }) {
  return (
    <ConfirmDialog
      title={props.title}
      message={props.message}
      onCancel={props.onCancel ?? vi.fn()}
      onConfirm={vi.fn()}
    >
      <button type="button">Body control</button>
    </ConfirmDialog>
  );
}

describe("ConfirmDialog", () => {
  it("uses unique title and description ids for concurrent instances", () => {
    render(
      <>
        <Dialog title="First title" message="First message" />
        <Dialog title="Second title" message="Second message" />
      </>,
    );
    const dialogs = screen.getAllByRole("alertdialog");
    const titleIds = dialogs.map((dialog) => dialog.getAttribute("aria-labelledby"));
    const messageIds = dialogs.map((dialog) => dialog.getAttribute("aria-describedby"));
    expect(new Set(titleIds).size).toBe(2);
    expect(new Set(messageIds).size).toBe(2);
    expect(within(dialogs[0]).getByText("First title")).toHaveAttribute(
      "id",
      titleIds[0],
    );
    expect(within(dialogs[1]).getByText("Second message")).toHaveAttribute(
      "id",
      messageIds[1],
    );
  });

  it("renders non-scrolling header/footer around the sole scrolling body", () => {
    render(<Dialog title="Bounded title" message="Long message" />);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.querySelectorAll(".confirm-dialog-header")).toHaveLength(1);
    expect(dialog.querySelectorAll(".confirm-dialog-body")).toHaveLength(1);
    expect(dialog.querySelectorAll(".confirm-dialog-actions")).toHaveLength(1);
    expect(
      within(dialog.querySelector(".confirm-dialog-body") as HTMLElement)
        .getByRole("button", { name: "Body control" }),
    ).toBeVisible();

    const css = readFileSync("src/styles/shared.css", "utf8");
    expect(css).toMatch(/\.confirm-dialog\s*\{[^}]*display:\s*flex;/s);
    expect(css).toMatch(
      /\.confirm-dialog\s*\{[^}]*max-height:\s*calc\(100dvh - 32px\);[^}]*overflow:\s*hidden;/s,
    );
    expect(css).toMatch(
      /\.confirm-dialog-body\s*\{[^}]*min-width:\s*0;[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto;/s,
    );
    const sidebarCss = readFileSync("src/components/sidebar/sidebar.css", "utf8");
    expect(sidebarCss).toMatch(
      /\.run-disclosure-summary\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/s,
    );
    expect(sidebarCss).toMatch(
      /\.run-disclosure-summary li\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/s,
    );
    expect(css).toMatch(/\.confirm-dialog-header[^}]*flex-shrink:\s*0;/s);
    expect(css).toMatch(/\.confirm-dialog-actions[^}]*flex-shrink:\s*0;/s);
  });

  it("restores focus to the exact invoker after cancellation unmounts it", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open dialog</button>
          {open ? (
            <ConfirmDialog
              title="Confirm"
              message="Continue?"
              onConfirm={() => setOpen(false)}
              onCancel={() => setOpen(false)}
            />
          ) : null}
        </>
      );
    }

    render(<Harness />);
    const invoker = screen.getByRole("button", { name: "Open dialog" });
    await user.click(invoker);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(invoker).toHaveFocus();
  });
});
