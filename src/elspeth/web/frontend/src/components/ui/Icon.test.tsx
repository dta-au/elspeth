import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Icon, type IconName } from "./Icon";

const ALL_NAMES: IconName[] = [
  "assistant",
  "check",
  "chevron-down",
  "chevron-up",
  "chevrons-left",
  "chevrons-right",
  "cross",
  "download",
  "eye",
  "folder",
  "key",
  "more",
  "pipeline",
  "play",
  "status-error",
  "status-pending",
  "status-ready",
  "status-unknown",
  "trash",
  "upload",
  "user",
];

describe("Icon", () => {
  it("renders non-empty geometry for every IconName (exhaustiveness net)", () => {
    // renderIcon is a switch with no default; a name added to the union but
    // not the switch would render an EMPTY svg — invisible, not a type error
    // at the call site. Pin that every name emits at least one shape.
    for (const name of ALL_NAMES) {
      const { container, unmount } = render(<Icon name={name} />);
      const svg = container.querySelector("svg")!;
      expect(svg, name).not.toBeNull();
      expect(
        svg.querySelectorAll("path, circle, rect, line, polyline, polygon").length,
        `Icon name="${name}" rendered no shapes`,
      ).toBeGreaterThan(0);
      unmount();
    }
  });

  it("stamps data-icon, hides from AT, and defaults to the 16px box", () => {
    const { container } = render(<Icon name="folder" />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("data-icon")).toBe("folder");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(svg.getAttribute("focusable")).toBe("false");
    expect(svg.getAttribute("width")).toBe("16");
    expect(svg.getAttribute("height")).toBe("16");
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(svg.getAttribute("stroke-width")).toBe("1.8");
  });

  it("lets consumers choose the rendered size via props (sizing contract)", () => {
    // The chat family renders this family at 20px (via .chat-input-icon CSS
    // or explicit props); the geometry stays in the 24-unit viewBox so the
    // effective stroke scales with the rendered size. Pin that width/height
    // spread through instead of being pinned to 16.
    const { container } = render(<Icon name="upload" width={20} height={20} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("20");
    expect(svg.getAttribute("height")).toBe("20");
  });

  it("merges the caller className after the ui-icon base", () => {
    const { container } = render(<Icon name="more" className="chat-input-icon" />);
    const svg = container.querySelector("svg")!;
    expect(svg.classList.contains("ui-icon")).toBe(true);
    expect(svg.classList.contains("chat-input-icon")).toBe(true);
  });

  describe("the overflow glyph (elspeth-b720e0b932)", () => {
    it("draws 'more' as three FILLED circles, never round-cap stroke dots", () => {
      // Zero-length round-cap strokes rasterise as hard squares at 1.5px and
      // carry an order of magnitude less ink than sibling glyphs, so the
      // control reads as disabled. The contract is explicit circles with an
      // own fill that beats the root's fill="none".
      const { container } = render(<Icon name="more" />);
      const svg = container.querySelector("svg")!;
      const circles = [...svg.querySelectorAll("circle")];
      expect(circles).toHaveLength(3);
      for (const circle of circles) {
        expect(circle.getAttribute("fill")).toBe("currentColor");
        expect(Number(circle.getAttribute("r"))).toBeGreaterThan(0);
      }
      // No path fallback — a zero-length "M5 12h.01" regression would come
      // back as a <path>.
      expect(svg.querySelector("path")).toBeNull();
    });
  });
});
