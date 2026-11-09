import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "./Button";

describe("Button", () => {
  it("renders the base .btn class, label, and type=button", () => {
    render(<Button>Run</Button>);
    const btn = screen.getByRole("button", { name: "Run" });
    expect(btn).toHaveClass("btn");
    expect(btn).toHaveAttribute("type", "button");
  });
  it("applies the variant modifier (secondary = no modifier)", () => {
    render(<Button variant="primary">Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toHaveClass("btn", "btn-primary");
  });
  it("uses the standalone compact base class", () => {
    render(<Button compact>x</Button>);
    const b = screen.getByRole("button", { name: "x" });
    expect(b).toHaveClass("btn-compact");
    expect(b).not.toHaveClass("btn");
  });
  it("forwards className and DOM props", () => {
    render(<Button className="x" disabled aria-label="lbl" />);
    const b = screen.getByRole("button", { name: "lbl" });
    expect(b).toHaveClass("x");
    expect(b).toBeDisabled();
  });

  it("forwards a ref to the underlying button element (focus management)", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<Button ref={ref}>Focus me</Button>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    ref.current!.focus();
    expect(ref.current).toHaveFocus();
  });

  it("preserves the Button display name through forwardRef", () => {
    expect(Button.displayName).toBe("Button");
  });

  it("honours an explicit type=submit (form submit buttons must pass it)", () => {
    render(<Button type="submit">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute(
      "type",
      "submit",
    );
  });

  describe("variant=bare", () => {
    it("emits only the caller's className — no .btn, no variant modifier", () => {
      render(
        <Button variant="bare" className="tab-trigger">
          Tab
        </Button>,
      );
      const b = screen.getByRole("button", { name: "Tab" });
      expect(b).toHaveClass("tab-trigger");
      expect(b).not.toHaveClass("btn");
      expect(b).not.toHaveClass("btn-compact");
    });

    it("emits no class attribute at all when className is absent", () => {
      render(<Button variant="bare">Plain</Button>);
      const b = screen.getByRole("button", { name: "Plain" });
      expect(b.getAttribute("class")).toBeNull();
    });

    it("ignores compact (compact only selects between base classes)", () => {
      render(
        <Button variant="bare" compact className="row-trigger">
          Row
        </Button>,
      );
      const b = screen.getByRole("button", { name: "Row" });
      expect(b.getAttribute("class")).toBe("row-trigger");
    });

    it("still contributes the type=button default and the icon slots", () => {
      render(
        <Button
          variant="bare"
          iconLeft={<span data-testid="l">L</span>}
          iconRight={<span data-testid="r">R</span>}
        >
          Mid
        </Button>,
      );
      const b = screen.getByRole("button");
      expect(b).toHaveAttribute("type", "button");
      expect(b).toHaveTextContent("Mid");
      expect(screen.getByTestId("l")).toBeInTheDocument();
      expect(screen.getByTestId("r")).toBeInTheDocument();
    });
  });
});
