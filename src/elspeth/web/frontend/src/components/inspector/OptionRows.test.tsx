import { act, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import { OptionRows } from "./OptionRows";

const OPTIONS = {
  profile: "sonnet",
  prompt_template: "Rate {{ row['case_study1'] }}",
  temperature: 0.2,
  schema: { mode: "observed", guaranteed_fields: ["id"] },
  interpretation_requirements: [{ id: "x", accepted_artifact_hash: "3876" + "a".repeat(60) }],
  blob_ref: "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
};

describe("OptionRows", () => {
  beforeEach(() => resetStore(usePreferencesStore));

  it("shows essential rows first, advanced behind a closed disclosure, and no raw JSON by default", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    const terms = within(region).getAllByRole("term").map((t) => t.textContent);
    expect(terms.slice(0, 3)).toEqual(["Prompt", "Model profile", "Row schema"]);
    expect(region.textContent).not.toMatch(/prompt_template|schema_mode/);
    const advanced = within(region).getByText("Advanced settings (1)").closest("details");
    expect(advanced).not.toHaveAttribute("open");
    expect(within(region).queryByText(/Raw options/)).not.toBeInTheDocument();
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
    expect(region.textContent).not.toMatch(/a{60}/);
  });

  it("with show_advanced on, opens the disclosure and offers the raw JSON", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    expect(within(region).getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
    expect(within(region).getByText("Raw options (JSON)")).toBeInTheDocument();
  });

  it("reacts when the preference flips on an already-mounted panel (the real user flow)", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    expect(screen.getByText("Advanced settings (1)").closest("details")).not.toHaveAttribute("open");
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(screen.getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Raw options (JSON)")).toBeInTheDocument();
  });

  it("renders a plain sentence for empty options", () => {
    render(<OptionRows options={{}} ariaLabel="gate options" />);
    expect(screen.getByText("No settings for this step.")).toBeInTheDocument();
  });
});
