import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AuditCharacteristicIcon } from "./AuditCharacteristicIcon";

describe("AuditCharacteristicIcon", () => {
  it("renders the label for a known flag", () => {
    render(<AuditCharacteristicIcon flag="io_read" />);
    expect(screen.getByText(/reads i\/?o/i)).toBeInTheDocument();
  });

  it("renders no emoji glyph span — the chip is its text label alone (elspeth-09a1a87051)", () => {
    // The former glyph column shipped nine emoji into a display:none span; the
    // span, the CSS rule hiding it, and the metadata column were deleted
    // TOGETHER so removing any one of them cannot silently re-arm the trap.
    const { container } = render(<AuditCharacteristicIcon flag="io_read" />);
    expect(container.querySelector(".audit-icon-glyph")).toBeNull();
    expect(container.textContent).toBe("reads I/O");
  });

  it("uses a positive-tone class for io_read", () => {
    const { container } = render(<AuditCharacteristicIcon flag="io_read" />);
    expect(container.firstChild).toHaveClass("audit-icon-positive");
  });

  it("uses an attention-tone class for external_call", () => {
    const { container } = render(<AuditCharacteristicIcon flag="external_call" />);
    expect(container.firstChild).toHaveClass("audit-icon-attention");
  });

  it("renders the tooltip on the title attribute", () => {
    render(<AuditCharacteristicIcon flag="quarantine" />);
    const el = screen.getByText(/quarantines/i);
    // Tooltip via title for keyboard / screen-reader access without
    // pulling in a tooltip library.
    expect(el.closest("[title]")?.getAttribute("title")).toMatch(/sink/i);
  });

  it("renders nothing for a flag outside the closed vocabulary — drift is the parity test's job, not a chip's (elspeth-0bfd019f68)", () => {
    // future_characteristic, not the deleted tests' future_flag_2027: the
    // digit-free form is what the rest of this wave's fixtures use, because
    // SNAKE_RE admits no digits (Global Constraints). It makes no difference
    // to toBeEmptyDOMElement here, and it keeps one flag spelling in the wave.
    const { container } = render(<AuditCharacteristicIcon flag="future_characteristic" />);
    expect(container).toBeEmptyDOMElement();
  });
});
