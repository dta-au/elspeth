import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CatalogButton } from "./CatalogButton";
import { OPEN_CATALOG_EVENT } from "@/lib/composer-events";

describe("CatalogButton", () => {
  it("renders a plain compact button whose accessible name IS the visible label", () => {
    render(<CatalogButton />);

    // WCAG 2.5.3 Label in Name: the popover-era aria-label
    // "Catalog (reference)" did not contain the visible "Plugin catalog",
    // so the accessible name is now the visible text itself. The
    // "Reference" meta chip stayed behind in the retired popover.
    const button = screen.getByRole("button", { name: "Plugin catalog" });
    expect(button).toHaveClass("btn-compact");
    expect(screen.queryByText("Reference")).toBeNull();
  });

  it("dispatches OPEN_CATALOG_EVENT on click", () => {
    const handler = vi.fn();
    window.addEventListener(OPEN_CATALOG_EVENT, handler);

    render(<CatalogButton />);
    fireEvent.click(screen.getByRole("button", { name: "Plugin catalog" }));

    expect(handler).toHaveBeenCalled();
    window.removeEventListener(OPEN_CATALOG_EVENT, handler);
  });
});
