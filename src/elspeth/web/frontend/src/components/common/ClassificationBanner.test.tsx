import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ClassificationBanner } from "./ClassificationBanner";

describe("ClassificationBanner", () => {
  it("renders the UNOFFICIAL marking with its level class", () => {
    render(<ClassificationBanner level="unofficial" />);
    const banner = screen.getByTestId("classification-banner");
    expect(banner).toHaveTextContent("UNOFFICIAL");
    expect(banner).toHaveClass(
      "classification-banner",
      "classification-banner--unofficial",
    );
  });

  it("renders the OFFICIAL marking with its level class", () => {
    render(<ClassificationBanner level="official" />);
    const banner = screen.getByTestId("classification-banner");
    expect(banner).toHaveTextContent("OFFICIAL");
    expect(banner).toHaveClass(
      "classification-banner",
      "classification-banner--official",
    );
  });

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
