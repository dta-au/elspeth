import { readFileSync } from "node:fs";

import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { AppHeader } from "./AppHeader";

// The ModelChip fetches /api/system/status on mount; give it a stable answer
// so the placement pin below is not racing an unmocked network call. All
// other client exports stay real.
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  fetchSystemStatus: vi.fn().mockResolvedValue({
    composer_available: true,
    composer_model: "anthropic/claude-sonnet-4.6",
    composer_provider: "openrouter",
    composer_reason: null,
    composer_missing_keys: [],
  }),
}));

describe("AppHeader", () => {
  it("sizes the application shell to the dynamic viewport with a legacy fallback", () => {
    const css = readFileSync("src/components/header/header.css", "utf8");
    const appRoot = css.match(/\.app-root\s*\{([^}]*)\}/s)?.[1];

    expect(appRoot).toBeDefined();
    expect(appRoot).toMatch(/height:\s*100vh;\s*height:\s*100dvh;/s);
  });

  it("uses one compact control height for the short desktop header and version trigger", () => {
    const css = readFileSync("src/components/header/header.css", "utf8");
    const shortHeightRule = css.match(
      /@media \(min-width: 961px\) and \(max-height: 800px\)\s*\{([\s\S]*?)\n\}/,
    )?.[1];
    expect(shortHeightRule).toBeDefined();
    expect(shortHeightRule).toMatch(
      /\.app-header\s*\{[^}]*height:\s*calc\(var\(--size-control-compact\) \+ 1px\);/s,
    );
    expect(shortHeightRule).toMatch(
      /\.header-session-switcher-trigger,\s*\.header-version-selector \.version-selector-trigger,\s*\.user-menu-trigger\s*\{[^}]*height:\s*var\(--size-control-compact\);[^}]*min-height:\s*var\(--size-control-compact\);/s,
    );
    expect(shortHeightRule).not.toMatch(
      /height:\s*calc\(var\(--size-header-height\) - var\(--space-sm\)\)/,
    );
  });

  it("renders the ELSPETH brand", () => {
    render(<AppHeader onOpenSettings={() => {}} onSignOut={() => {}} />);
    expect(screen.getByText(/ELSPETH/i)).toBeInTheDocument();
  });

  it("renders the session switcher", () => {
    // "No session" is the no-active-session trigger label — the switcher no
    // longer mints a competing "Untitled" default (elspeth-ef8c18a6cb).
    render(<AppHeader onOpenSettings={() => {}} onSignOut={() => {}} />);
    expect(
      screen.getByRole("button", { name: /session switcher: no session/i }),
    ).toBeInTheDocument();
  });

  it("renders the user menu", () => {
    render(<AppHeader onOpenSettings={() => {}} onSignOut={() => {}} />);
    expect(screen.getByRole("button", { name: /account/i })).toBeInTheDocument();
  });

  it("carries the composer-model chip — relocated from the chat headers (elspeth-8fa71e6d15)", async () => {
    // Identity chrome lives in the identity-chrome region: the chip's
    // previous home was a 360px authoring column that truncated it to "Mo…".
    // ChatPanel.test.tsx pins the corresponding absence in both chat headers.
    const { container } = render(
      <AppHeader onOpenSettings={() => {}} onSignOut={() => {}} />,
    );
    await waitFor(() => {
      expect(
        screen.getByLabelText("Composer model: anthropic/claude-sonnet-4.6"),
      ).toBeInTheDocument();
    });
    expect(container.querySelector(".app-header-left .chat-model-chip")).not.toBeNull();
  });
});
