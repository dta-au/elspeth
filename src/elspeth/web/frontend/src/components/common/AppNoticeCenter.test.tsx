import { useState } from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { axe } from "@/test/a11y/axe-config";

import {
  AppNoticeCenter,
  type AppNotice,
} from "./AppNoticeCenter";

function notice(
  kind: AppNotice["kind"],
  role: AppNotice["role"],
  label: string,
  action?: ReactNode,
): AppNotice {
  return { kind, role, content: <span>{label}</span>, action };
}

describe("AppNoticeCenter", () => {
  it("uses the fixed application priority and keeps equal-priority input stable", async () => {
    const notices: AppNotice[] = [
      notice("composer-unavailable", "status", "Composer unavailable"),
      notice("redirect", "alert", "Redirect one"),
      notice("stale-build", "status", "Stale build"),
      notice("preferences", "alert", "Preferences failed"),
      notice("redirect", "alert", "Redirect two"),
      notice("backend-unavailable", "alert", "Backend unavailable"),
    ];

    render(<AppNoticeCenter notices={notices} />);

    const primary = screen.getByTestId("app-notice-primary");
    expect(primary).toHaveAttribute("role", "alert");
    expect(within(primary).getByText("Backend unavailable")).toBeVisible();
    expect(within(primary).queryByText("Preferences failed")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: "5 more notices" }),
    );
    const items = within(screen.getByRole("region", { name: "All notices" }))
      .getAllByTestId("app-notice-additional");
    expect(items.map((item) => item.textContent)).toEqual([
      "Backend unavailable",
      "Preferences failed",
      "Redirect one",
      "Redirect two",
      "Stale build",
      "Composer unavailable",
    ]);
  });

  it("preserves notice roles and actions without duplicating hidden messages in live summaries", async () => {
    const retry = vi.fn();
    const configure = vi.fn();
    render(
      <AppNoticeCenter
        notices={[
          notice(
            "backend-unavailable",
            "alert",
            "Backend unavailable",
            <button type="button" onClick={retry}>Retry</button>,
          ),
          notice(
            "composer-unavailable",
            "status",
            "Composer unavailable",
            <button type="button" onClick={configure}>Configure</button>,
          ),
          notice("preferences", "alert", "Preferences failed"),
        ]}
      />,
    );

    const summary = screen.getByText(
      "1 additional urgent notice is available.",
    );
    expect(summary).toHaveAttribute("role", "alert");
    expect(summary).not.toHaveTextContent("Preferences failed");
    expect(screen.getAllByText("Backend unavailable")).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
    await userEvent.click(
      screen.getByRole("button", { name: "2 more notices" }),
    );
    const popover = screen.getByRole("region", { name: "All notices" });
    expect(within(popover).getByText("Preferences failed").closest("[role]"))
      .toHaveAttribute("role", "alert");
    expect(within(popover).getByText("Composer unavailable").closest("[role]"))
      .toHaveAttribute("role", "status");
    await userEvent.click(within(popover).getByRole("button", { name: "Configure" }));
    expect(configure).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("region", { name: "All notices" })).toBeNull();
    expect(screen.getByRole("button", { name: "2 more notices" })).toHaveFocus();
  });

  it("closes on Escape and outside pointer input and restores the exact invoker", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <button type="button">Outside</button>
        <AppNoticeCenter
          notices={[
            notice("backend-unavailable", "alert", "Backend"),
            notice("preferences", "alert", "Preferences"),
          ]}
        />
      </div>,
    );
    const invoker = screen.getByRole("button", { name: "1 more notice" });

    await user.click(invoker);
    expect(screen.getByRole("region", { name: "All notices" })).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "All notices" })).toBeNull();
    expect(invoker).toHaveFocus();

    await user.click(invoker);
    await user.click(screen.getByRole("button", { name: "Outside" }));
    expect(screen.queryByRole("region", { name: "All notices" })).toBeNull();
    expect(invoker).toHaveFocus();
  });

  it("restores focus after commit to the primary notice when More unmounts", async () => {
    const user = userEvent.setup();
    const primaryNotice = notice(
      "backend-unavailable",
      "alert",
      "Backend",
    );
    const { rerender } = render(
      <AppNoticeCenter
        notices={[
          primaryNotice,
          notice("preferences", "alert", "Preferences"),
        ]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "1 more notice" }));
    rerender(<AppNoticeCenter notices={[primaryNotice]} />);

    const primary = screen.getByTestId("app-notice-primary");
    await waitFor(() => expect(primary).toHaveFocus());
    expect(screen.queryByRole("region", { name: "All notices" })).toBeNull();
  });

  it("uses the primary notice then Composer main as stable action focus fallbacks", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [notices, setNotices] = useState<AppNotice[]>([
        notice(
          "backend-unavailable",
          "alert",
          "Backend",
          <button type="button" onClick={() => setNotices([])}>
            Clear all notices
          </button>,
        ),
        notice(
          "preferences",
          "alert",
          "Preferences",
          <button
            type="button"
            onClick={() =>
              setNotices((current) =>
                current.filter((item) => item.kind !== "preferences"),
              )
            }
          >
            Dismiss preferences
          </button>,
        ),
      ]);
      return (
        <>
          <main id="composer-main" tabIndex={-1}>
            Composer main
          </main>
          <AppNoticeCenter notices={notices} />
        </>
      );
    }

    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "1 more notice" }));
    await user.click(
      screen.getByRole("button", { name: "Dismiss preferences" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("app-notice-primary")).toHaveFocus(),
    );

    await user.click(screen.getByRole("button", { name: "Clear all notices" }));
    await waitFor(() =>
      expect(document.getElementById("composer-main")).toHaveFocus(),
    );
  });

  it("renders no banner row when there are no active notices", () => {
    const { container } = render(<AppNoticeCenter notices={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("has no axe violations with the notice disclosure open", async () => {
    const { container } = render(
      <AppNoticeCenter
        notices={[
          notice("backend-unavailable", "alert", "Backend unavailable"),
          notice("preferences", "alert", "Preferences failed"),
        ]}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "1 more notice" }),
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
