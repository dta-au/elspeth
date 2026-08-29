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

  // elspeth-b9ebdf9011 review: the essential `path` row must mask a
  // blob-backed source's `blob:<ref>` sentinel (mirrors SchemaFormTurn.tsx's
  // `maskBlobRef`) — the raw UUID belongs ONLY in a `title` attribute, never
  // in visible text, and this is the one place that guard is unit-tested
  // directly (previously covered only transitively, and only for textContent
  // absence, via PipelineSpecView's integration test). GraphView's node
  // inspector renders `options.path` through this same component, so the
  // fix (and its test) live here rather than in either consumer.
  it("masks a blob-backed path sentinel: friendly text visible, raw sentinel only in the title attribute", () => {
    const options = { path: "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0" };
    render(<OptionRows options={options} ariaLabel="source options" />);
    const region = screen.getByRole("region", { name: "source options" });

    const masked = within(region).getByText("Uploaded sample data");
    expect(masked).toBeInTheDocument();
    expect(masked).toHaveAttribute(
      "title",
      "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
    );
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
  });

  // Guard the other direction: an ordinary (non-blob) path must render
  // literally, so the mask can't have degenerated into hiding every path.
  it("does not mask a path value that is not a blob:<ref> sentinel", () => {
    const options = { path: "project_brief_urls.json" };
    render(<OptionRows options={options} ariaLabel="source options" />);
    const region = screen.getByRole("region", { name: "source options" });

    expect(within(region).getByText("project_brief_urls.json")).toBeInTheDocument();
    expect(within(region).queryByText("Uploaded sample data")).not.toBeInTheDocument();
  });

  // Live-check finding (session 39578c6f, Spec tab, show_advanced off):
  // `guaranteed_fields` rendered raw in a <dt> under the source's `schema`
  // value — a nested object walked by ConfigValue's own recursion
  // (ConfigRows.tsx), not by OptionRows' pick()/optionLabel top-level
  // relabeling. Structural KEYS get the same titleCaseLabel humanising;
  // VALUES (the reader's own data, e.g. column names) must stay verbatim.
  it("humanises nested structural keys inside a schema-shaped option value, keeping the raw key recoverable in title", () => {
    const options = {
      schema: { mode: "observed", guaranteed_fields: ["id", "email"] },
    };
    render(<OptionRows options={options} ariaLabel="source options" />);
    const region = screen.getByRole("region", { name: "source options" });

    const nestedTerm = within(region).getByText("Guaranteed Fields");
    expect(nestedTerm).toBeInTheDocument();
    expect(nestedTerm).toHaveAttribute("title", "guaranteed_fields");
    expect(within(region).queryByText("guaranteed_fields")).not.toBeInTheDocument();
    expect(within(region).getByText("Mode")).toBeInTheDocument();
    // Values are the reader's own data (field names here), never relabeled.
    expect(within(region).getByText("id")).toBeInTheDocument();
    expect(within(region).getByText("email")).toBeInTheDocument();
  });

  // Round-3 review finding: the round-2 fix humanised EVERY nested dict's
  // keys unconditionally, which is correct for `schema` (ELSPETH's own
  // vocabulary) but wrong for `field_mapping` — a `dict[reader's own column
  // name, target name]` on every tabular source/sink. Both the key AND the
  // value there are the reader's data; "weird_header" relabeled to "Weird
  // Header" would be actively misleading (a column that isn't really named
  // "Weird Header"). Fixed by making the humanising fail CLOSED: on by
  // default only under an explicit STRUCTURAL_OPTION_CONTAINER_KEYS
  // allowlist (ConfigRows.tsx), off for everything else, `field_mapping`
  // included.
  it("renders field_mapping's reader-authored keys verbatim, never humanised", () => {
    const options = { field_mapping: { weird_header: "b" } };
    render(<OptionRows options={options} ariaLabel="source options" />);
    const region = screen.getByRole("region", { name: "source options" });

    expect(within(region).getByText("weird_header")).toBeInTheDocument();
    expect(within(region).queryByText("Weird Header")).not.toBeInTheDocument();
    expect(region.textContent).not.toMatch(/Weird Header/);
  });

  // The allowlist must be an allowlist, not a denylist inferred from what's
  // already known to be wrong: an option key nobody has put on
  // STRUCTURAL_OPTION_CONTAINER_KEYS must default to verbatim even though
  // it happens to nest a dict, so a brand-new user-keyed option is safe on
  // day one with no code change required.
  it("renders an unlisted nested container's keys verbatim (fail-closed default)", () => {
    const options = { lookups: { customer_id: { entity: "contact" } } };
    render(<OptionRows options={options} ariaLabel="source options" />);
    const region = screen.getByRole("region", { name: "source options" });

    expect(within(region).getByText("customer_id")).toBeInTheDocument();
    expect(within(region).queryByText("Customer Id")).not.toBeInTheDocument();
    expect(within(region).getByText("entity")).toBeInTheDocument();
    expect(within(region).queryByText("Entity")).not.toBeInTheDocument();
  });
});
