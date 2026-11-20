import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BreakableName } from "./BreakableName";

describe("BreakableName", () => {
  it("offers a break after each separator and keeps the text unchanged", () => {
    const { container } = render(<p><BreakableName name="quarterly_review.v7-final/prompt.txt" /></p>);
    expect(container.textContent).toBe("quarterly_review.v7-final/prompt.txt");
    expect(container.querySelectorAll("wbr")).toHaveLength(5);
    expect(container.innerHTML).toBe("<p>quarterly_<wbr>review.<wbr>v7-<wbr>final/<wbr>prompt.<wbr>txt</p>");
  });

  it("adds nothing to a name without separators", () => {
    const { container } = render(<p><BreakableName name="prompt" /></p>);
    expect(container.innerHTML).toBe("<p>prompt</p>");
  });
});
