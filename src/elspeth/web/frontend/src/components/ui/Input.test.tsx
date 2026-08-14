import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Input } from "./Input";

describe("Input", () => {
  it("renders an input element carrying the .input class", () => {
    const { container } = render(<Input placeholder="search" />);
    const input = container.querySelector("input");
    expect(input).not.toBeNull();
    expect(input).toHaveClass("input");
  });

  it("associates the label with the control so getByLabelText resolves it", () => {
    render(<Input label="Username" />);
    const control = screen.getByLabelText("Username");
    expect(control.tagName).toBe("INPUT");
    expect(control).toHaveClass("input");
  });

  it("honours an explicit id for the label association", () => {
    render(<Input label="Token" id="api-token" />);
    const control = screen.getByLabelText("Token");
    expect(control).toHaveAttribute("id", "api-token");
  });

  it("renders the field label and hint chrome", () => {
    const { container } = render(<Input label="Path" hint="absolute path" />);
    expect(container.querySelector(".field-label")).not.toBeNull();
    expect(container.querySelector(".field-hint")).not.toBeNull();
    expect(screen.getByText("absolute path")).toHaveClass("field-hint");
  });

  it("applies .input-mono when mono is set", () => {
    const { container } = render(<Input mono />);
    expect(container.querySelector("input")).toHaveClass("input-mono");
  });

  it("does not apply .input-mono by default", () => {
    const { container } = render(<Input />);
    expect(container.querySelector("input")).not.toHaveClass("input-mono");
  });

  it("merges a caller-supplied className with .input", () => {
    const { container } = render(<Input className="wide" />);
    const input = container.querySelector("input");
    expect(input).toHaveClass("input");
    expect(input).toHaveClass("wide");
  });

  it("forwards value, required, and other DOM props", () => {
    render(
      <Input
        label="Email"
        value="user@example.com"
        required
        type="email"
        autoComplete="email"
        onChange={() => {}}
      />,
    );
    const control = screen.getByLabelText("Email");
    expect(control).toHaveValue("user@example.com");
    expect(control).toBeRequired();
    expect(control).toHaveAttribute("type", "email");
    expect(control).toHaveAttribute("autocomplete", "email");
  });

  it("forwards onChange when the user types", async () => {
    const handleChange = vi.fn();
    render(<Input label="Name" onChange={handleChange} />);
    await userEvent.type(screen.getByLabelText("Name"), "ab");
    expect(handleChange).toHaveBeenCalled();
  });

  it("renders a bare control when neither label nor hint is given", () => {
    const { container } = render(<Input />);
    expect(container.querySelector("div")).toBeNull();
    expect(container.querySelector("input")).not.toBeNull();
  });

  it("forwards a ref to the underlying input element (focus management)", () => {
    const ref = createRef<HTMLInputElement>();
    render(<Input ref={ref} />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
    ref.current!.focus();
    expect(ref.current).toHaveFocus();
  });

  it("forwards a ref to the control, not the wrapper, when label/hint chrome renders", () => {
    const ref = createRef<HTMLInputElement>();
    render(<Input ref={ref} label="Name" hint="as registered" />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
    expect(ref.current).toBe(screen.getByLabelText("Name"));
  });

  it("preserves the Input display name through forwardRef", () => {
    expect(Input.displayName).toBe("Input");
  });

  describe("non-text types skip the .input text-field class", () => {
    it.each(["checkbox", "radio", "range", "file", "color"] as const)(
      "type=%s does not receive .input",
      (type) => {
        const { container } = render(<Input type={type} />);
        const input = container.querySelector("input");
        expect(input).toHaveAttribute("type", type);
        expect(input).not.toHaveClass("input");
      },
    );

    it("emits no class attribute at all for a non-text type without className", () => {
      const { container } = render(<Input type="checkbox" />);
      expect(container.querySelector("input")!.getAttribute("class")).toBeNull();
    });

    it("still merges the caller's className for non-text types", () => {
      const { container } = render(
        <Input type="checkbox" className="toggle" />,
      );
      const input = container.querySelector("input");
      expect(input).toHaveClass("toggle");
      expect(input).not.toHaveClass("input");
    });

    it("still associates the label with a non-text control", () => {
      render(<Input type="checkbox" label="Enable retries" />);
      const control = screen.getByLabelText("Enable retries");
      expect(control).toHaveAttribute("type", "checkbox");
    });

    it("keeps explicit input-mono even on a non-text type (opt-in only)", () => {
      const { container } = render(<Input type="range" mono />);
      const input = container.querySelector("input");
      expect(input).toHaveClass("input-mono");
      expect(input).not.toHaveClass("input");
    });

    it.each(["text", "email", "password", "search", "number"] as const)(
      "text-shaped type=%s still receives .input",
      (type) => {
        const { container } = render(<Input type={type} />);
        expect(container.querySelector("input")).toHaveClass("input");
      },
    );
  });
});
