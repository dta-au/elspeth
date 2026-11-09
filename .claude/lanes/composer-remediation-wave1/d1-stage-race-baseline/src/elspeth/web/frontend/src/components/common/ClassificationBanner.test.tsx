import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ClassificationBanner,
  type ClassificationBannerLevel,
} from "./ClassificationBanner";

// Marking text is pinned in its standard PSPF form — mixed-case
// "Sensitive", double-slash caveat delimiter — because the CSS applies no
// text-transform and a drive-by uppercase would rewrite the marking into a
// non-standard rendering.
const LEVELS: ReadonlyArray<{
  level: ClassificationBannerLevel;
  text: string;
  className: string;
}> = [
  {
    level: "unofficial",
    text: "UNOFFICIAL",
    className: "classification-banner--unofficial",
  },
  {
    level: "official",
    text: "OFFICIAL",
    className: "classification-banner--official",
  },
  {
    level: "official_sensitive",
    text: "OFFICIAL: Sensitive",
    className: "classification-banner--official-sensitive",
  },
  {
    level: "protected",
    text: "PROTECTED",
    className: "classification-banner--protected",
  },
  {
    level: "protected_cabinet",
    text: "PROTECTED//CABINET",
    className: "classification-banner--protected-cabinet",
  },
];

describe("ClassificationBanner", () => {
  it.each(LEVELS)(
    "renders the $level marking as $text with its level class",
    ({ level, text, className }) => {
      render(<ClassificationBanner level={level} />);
      const banner = screen.getByTestId("classification-banner");
      // Exact text, not a substring match: "OFFICIAL" must not pass on an
      // "OFFICIAL: Sensitive" or "UNOFFICIAL" rendering.
      expect(banner.textContent).toBe(text);
      expect(banner).toHaveClass("classification-banner", className);
    },
  );

  it("carries no live-region or landmark semantics", () => {
    // The marking is static page identity. The reserved band's urgent
    // tenants (app notices, run outcome) own the live-region semantics; a
    // role here would make singular getByRole queries ambiguous and announce
    // ambient identity as news (recent-code-hints 2026-08-13 convention).
    render(<ClassificationBanner level="unofficial" />);
    const banner = screen.getByTestId("classification-banner");
    expect(banner).not.toHaveAttribute("role");
    expect(banner).not.toHaveAttribute("aria-live");
  });
});
