import { readFileSync } from "node:fs";

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AppHeader } from "./AppHeader";

describe("AppHeader", () => {
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
});
