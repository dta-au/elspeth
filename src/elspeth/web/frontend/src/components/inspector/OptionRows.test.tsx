import { act, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePreferencesStore } from "@/stores/preferencesStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";
import { resetStore } from "@/test/store-helpers";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import { OptionRows } from "./OptionRows";

const OPTIONS = {
  profile: "sonnet",
  prompt_template: "Rate {{ row['case_study1'] }}",
  temperature: 0.2,
  max_retries: 3,
  schema: { mode: "observed", guaranteed_fields: ["id"] },
  interpretation_requirements: [{ id: "x", accepted_artifact_hash: "3876" + "a".repeat(60) }],
  blob_ref: "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
};

// Top-level (module-scope) beforeEach, not nested in a describe: OptionRows
// reads BOTH stores, and the catalog-tier-ordering describe below is a
// sibling of describe("OptionRows", ...), not nested inside it, so a reset
// scoped only to that inner describe would not reach it and state would leak
// across the file's two describe blocks.
beforeEach(() => {
  resetStore(usePreferencesStore);
  resetStore(usePluginCatalogStore);
});

describe("OptionRows", () => {
  it("shows essential rows first, advanced behind a closed disclosure, and no raw JSON by default", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    const terms = within(region).getAllByRole("term").map((t) => t.textContent);
    expect(terms.slice(0, 3)).toEqual(["User prompt", "Model profile", "Row schema"]);
    expect(region.textContent).not.toMatch(/prompt_template|schema_mode/);
    const advanced = within(region).getByText("Advanced settings (2)").closest("details");
    expect(advanced).not.toHaveAttribute("open");
    expect(within(region).queryByText(/Raw options/)).not.toBeInTheDocument();
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
    expect(region.textContent).not.toMatch(/a{60}/);
  });

  it("with show_advanced on, opens the disclosure and offers the raw JSON", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    expect(within(region).getByText("Advanced settings (2)").closest("details")).toHaveAttribute("open");
    expect(within(region).getByText("Raw options (JSON)")).toBeInTheDocument();
  });

  it("reacts when the preference flips on an already-mounted panel (the real user flow)", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    expect(screen.getByText("Advanced settings (2)").closest("details")).not.toHaveAttribute("open");
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(screen.getByText("Advanced settings (2)").closest("details")).toHaveAttribute("open");
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

describe("catalog-tier ordering (elspeth-a6ea581e8a follow-up)", () => {
  const LLM_SCHEMA = {
    name: "llm",
    plugin_type: "transform",
    description: "",
    json_schema: {},
    knob_schema: {
      // `schema` is listed BEFORE `profile` on purpose: the prompt pair leads
      // every llm step regardless of partition, so the schema-order vs
      // fallback-order oracle below rides on these two keys instead.
      fields: [
        { name: "schema", tier: "common" },
        { name: "profile", tier: "common" },
        { name: "prompt_template", tier: "common" },
        { name: "temperature", tier: "advanced" },
      ],
    },
  } as const;
  const seedCatalog = (schemas: Record<string, unknown>) =>
    usePluginCatalogStore.setState({ key: "alice:fp-1", principal: "alice", fingerprint: "fp-1", schemas } as never);

  const DISCRIMINATED_TIER_SCHEMA = {
    name: "llm",
    plugin_type: "transform",
    description: "",
    json_schema: {},
    knob_schema: {
      fields: [
        { name: "provider", tier: "common" },
        {
          name: "max_tokens",
          tier: "advanced",
          visible_when: { field: "provider", equals: "azure" },
        },
        {
          name: "max_tokens",
          tier: "common",
          visible_when: { field: "provider", equals: "gateway" },
        },
      ],
    },
  } as const;

  it.each([
    ["azure", true],
    ["gateway", false],
  ] as const)("uses the active %s provider variant's max_tokens tier", (provider, maxTokensIsAdvanced) => {
    seedCatalog({ "transform:llm": DISCRIMINATED_TIER_SCHEMA });
    render(
      <OptionRows
        options={{ provider, max_tokens: 32 }}
        ariaLabel={`${provider} options`}
        plugin={{ kind: "transform", name: "llm" }}
      />,
    );
    const region = screen.getByRole("region", { name: `${provider} options` });
    const visibleTerms = within(region)
      .getAllByRole("term")
      .filter((term) => term.closest("details") === null)
      .map((term) => term.textContent);

    if (maxTokensIsAdvanced) {
      expect(visibleTerms).toEqual(["System prompt", "User prompt", "Provider"]);
      const advanced = within(region).getByText("Advanced settings (1)").closest("details");
      expect(within(advanced as HTMLElement).getByText("Max Tokens")).toBeInTheDocument();
    } else {
      expect(visibleTerms).toEqual(["System prompt", "User prompt", "Provider", "Max Tokens"]);
      expect(within(region).queryByText(/Advanced settings/)).not.toBeInTheDocument();
    }
  });

  it("orders visible rows by the schema and sends advanced-tier + unknown keys to the disclosure", () => {
    seedCatalog({ "transform:llm": LLM_SCHEMA });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    const region = screen.getByRole("region", { name: "assess options" });
    // `.graph-config-nested` excluded: OPTIONS.schema is a record, and ConfigValue
    // renders its keys as nested <dt>s (ConfigRows.tsx:41-52) in the visible partition.
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    // The prompt pair leads; the rest follow schema field order — DIFFERENT
    // from the fallback's label-map order (["Model profile", "Row schema"]);
    // this is the oracle that distinguishes the two partitions.
    expect(visibleTerms).toEqual(["System prompt", "User prompt", "Row schema", "Model profile"]);
    const advanced = within(region).getByText("Advanced settings (2)").closest("details") as HTMLElement;
    expect(within(advanced).getByText("Temperature")).toBeInTheDocument(); // advanced tier
    expect(within(advanced).getByText("Max Retries")).toBeInTheDocument(); // unknown to the schema
    expect(region.textContent).not.toMatch(/blob_ref|interpretation_requirements/);
  });

  // Review fix round 1: LLM_SCHEMA above never tiers a field "essential", so
  // the "essentials first, then commons, each in schema order" rule had no
  // regression pin — a regression that dropped the essentials branch, or
  // interleaved essentials and commons instead of ordering essentials ahead,
  // would not have been caught. This fixture's schema order deliberately
  // puts the essential field (schema) LAST of the visible three, behind a
  // common (profile), so the assertion only passes if tier — not schema
  // position — decides who goes first. (The essential field used to be
  // prompt_template; the prompt pair now leads every llm step outside the
  // partition, so it can no longer carry this oracle.)
  it("orders an essential-tier field ahead of commons even when it comes later in schema order", () => {
    const schemaWithEssential = {
      name: "llm",
      plugin_type: "transform",
      description: "",
      json_schema: {},
      knob_schema: {
        fields: [
          { name: "profile", tier: "common" },
          { name: "prompt_template", tier: "common" },
          { name: "schema", tier: "essential" },
          { name: "temperature", tier: "advanced" },
        ],
      },
    } as const;
    seedCatalog({ "transform:llm": schemaWithEssential });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    // schema (essential) jumps ahead of profile (common), even though schema
    // order lists it after profile.
    expect(visibleTerms).toEqual(["System prompt", "User prompt", "Row schema", "Model profile"]);
  });

  // Live regression (build index-D3qXar6h.js, session 39578c6f): the operator
  // policy view for `transform:llm` returned all 14 knob fields with NO `tier`
  // on any of them, because web/plugin_policy/profiles.py hand-builds that
  // projection. With the schema cached and tier read strictly, every visible
  // partition emptied and the prompt sank into "Advanced settings (5)" — worse
  // than the uncached fallback. A field the catalog KNOWS but does not tier is
  // visible; only keys the schema does not list at all are advanced.
  it("treats an untiered schema field as common — the live untiered llm policy view keeps every known key visible", () => {
    const untieredSchema = {
      name: "llm",
      plugin_type: "transform",
      description: "",
      json_schema: {},
      knob_schema: {
        fields: [
          { name: "profile" },
          { name: "prompt_template" },
          { name: "temperature" },
          { name: "schema" },
        ],
      },
    } as const;
    seedCatalog({ "transform:llm": untieredSchema });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    // Schema field order, all four present keys — including `temperature`,
    // which the tiered LLM_SCHEMA above sends to the disclosure.
    expect(visibleTerms).toEqual(["System prompt", "User prompt", "Model profile", "Temperature", "Row schema"]);
    // The disclosure holds ONLY the key the schema does not list. An absent
    // tier promotes a known field; it does not promote an unknown one.
    const advanced = within(region).getByText("Advanced settings (1)").closest("details") as HTMLElement;
    expect(within(advanced).getByText("Max Retries")).toBeInTheDocument();
    expect(within(advanced).queryByText("Temperature")).not.toBeInTheDocument();
  });

  it("falls back to the static split when the schema is not cached (regression pin — green before this task)", () => {
    const { container } = render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    // The value cells are an identifier surface BY DESIGN at this component:
    // `prompt_template` is in FALLBACK_VISIBLE_OPTION_KEYS under the reader
    // label "User prompt", so the wave deliberately renders authored prompt text —
    // `Rate {{ row['case_study1'] }}` here — to the reader. An underscore
    // inside content the USER wrote is not ELSPETH leaking an identifier, and
    // the pin cannot tell the two apart. Scanning the <dt> labels is what this
    // call is for: no raw option key reaches a visible label, and the internal
    // keys (blob_ref, interpretation_requirements) stay hidden entirely.
    // The blob-path sentinel rule is NOT weakened by this exemption — it
    // carries its own direct `region.textContent` assertions above.
    expectNoIdentifiersInDefaultDom(container, { allowSelectors: ["dl.graph-config-rows dd"] });
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    expect(visibleTerms).toEqual(["System prompt", "User prompt", "Model profile", "Row schema"]);
  });

  it("re-partitions when the catalog loads after mount (no request is made before the catalog has a key)", () => {
    const loadSchema = vi.fn().mockResolvedValue(undefined);
    usePluginCatalogStore.setState({ loadSchema } as never);
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    expect(loadSchema).not.toHaveBeenCalled(); // key is null: the store would no-op; we don't even ask
    act(() => seedCatalog({ "transform:llm": LLM_SCHEMA }));
    expect(loadSchema).toHaveBeenCalledWith("transform", "llm");
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    expect(visibleTerms).toEqual(["System prompt", "User prompt", "Row schema", "Model profile"]);
  });

  it("masks a blob:<ref> path even when the catalog tiers `path` advanced (masking binds to the value, not the partition)", () => {
    seedCatalog({
      "source:csv": { ...LLM_SCHEMA, name: "csv", plugin_type: "source", knob_schema: { fields: [{ name: "path", tier: "advanced" }] } },
    });
    render(
      <OptionRows
        options={{ path: "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0" }}
        ariaLabel="source options"
        plugin={{ kind: "source", name: "csv" }}
      />,
    );
    const region = screen.getByRole("region", { name: "source options" });
    const advanced = within(region).getByText("Advanced settings (1)").closest("details") as HTMLElement;
    expect(within(advanced).getByText("Uploaded sample data")).toHaveAttribute("title", "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0");
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
  });
});

// Session 60ab6a67: an llm step authored with a user prompt only rendered a
// lone "Prompt" row, so the box gave no sign a role was missing. Both roles
// are always shown for an llm step, system first, present or not.
describe("llm prompt roles", () => {
  const LLM = { kind: "transform", name: "llm" } as const;

  function roleRows(region: HTMLElement): Array<[string | null, string | null]> {
    const block = region.querySelector(".option-rows-prompt-roles") as HTMLElement;
    expect(block).not.toBeNull();
    return Array.from(block.querySelectorAll("dt")).map((dt) => [dt.textContent, dt.nextElementSibling?.textContent ?? null]);
  }

  it("leads with System prompt then User prompt when both are authored", () => {
    render(
      <OptionRows
        options={{ profile: "sonnet", prompt_template: "Name a colour pair for {{ row.room }}", system_prompt: "You are a talented interior decorator." }}
        ariaLabel="decorator options"
        plugin={LLM}
      />,
    );
    const region = screen.getByRole("region", { name: "decorator options" });
    expect(roleRows(region)).toEqual([
      ["System prompt", "You are a talented interior decorator."],
      ["User prompt", "Name a colour pair for {{ row.room }}"],
    ]);
    const terms = within(region).getAllByRole("term").map((t) => t.textContent);
    expect(terms.slice(0, 2)).toEqual(["System prompt", "User prompt"]);
    expect(terms.filter((t) => t === "System prompt" || t === "User prompt")).toHaveLength(2);
  });

  it.each([[undefined], [null], [""], ["   \n"]])("names a missing system prompt instead of dropping the row (%j)", (systemPrompt) => {
    render(
      <OptionRows
        options={{ profile: "sonnet", prompt_template: "Classify {{ row.text }}", system_prompt: systemPrompt }}
        ariaLabel="arm options"
        plugin={LLM}
      />,
    );
    const rows = roleRows(screen.getByRole("region", { name: "arm options" }));
    expect(rows[0]).toEqual(["System prompt", "Not set. Every LLM step needs a system prompt."]);
    expect(rows[1]).toEqual(["User prompt", "Classify {{ row.text }}"]);
  });

  it("names a missing user prompt", () => {
    render(<OptionRows options={{ system_prompt: "You classify tickets." }} ariaLabel="arm options" plugin={LLM} />);
    const rows = roleRows(screen.getByRole("region", { name: "arm options" }));
    expect(rows[1]).toEqual(["User prompt", "Not set. Every LLM step needs a user prompt."]);
  });

  it.each([
    ["mapping form", { tone: { template: "Tone of {{ row.text }}?" }, urgency: { template: "Urgency of {{ row.text }}?" } }],
    ["list form", [{ name: "tone", template: "Tone of {{ row.text }}?" }, { name: "urgency", template: "Urgency of {{ row.text }}?" }]],
  ])("shows one system prompt and every query's user prompt for a multi-query step (%s)", (_form, queries) => {
    render(<OptionRows options={{ system_prompt: "You assess tickets.", queries }} ariaLabel="multi options" plugin={LLM} />);
    const region = screen.getByRole("region", { name: "multi options" });
    expect(roleRows(region)).toEqual([
      ["System prompt", "You assess tickets."],
      ["User prompt", "toneTone of {{ row.text }}?"],
      ["User prompt", "urgencyUrgency of {{ row.text }}?"],
    ]);
    // The per-query prompts sit in the always-open block, never only behind
    // the collapsed disclosure the uncached fallback sends `queries` to.
    const block = region.querySelector(".option-rows-prompt-roles") as HTMLElement;
    expect(block.closest("details")).toBeNull();
    expect(Array.from(block.querySelectorAll(".option-rows-query-name")).map((n) => n.textContent)).toEqual(["tone", "urgency"]);
  });

  it("shows the shared fallback for a query without its own template, and names one with no user prompt at all", () => {
    const { unmount } = render(
      <OptionRows
        options={{ system_prompt: "You assess tickets.", prompt_template: "Assess {{ row.text }}", queries: { tone: { template: "Tone?" }, urgency: {} } }}
        ariaLabel="multi options"
        plugin={LLM}
      />,
    );
    expect(roleRows(screen.getByRole("region", { name: "multi options" })).slice(1)).toEqual([
      ["User prompt", "toneTone?"],
      ["User prompt", "urgencyAssess {{ row.text }}"],
    ]);
    unmount();
    render(<OptionRows options={{ system_prompt: "You assess tickets.", queries: { urgency: {} } }} ariaLabel="multi options" plugin={LLM} />);
    expect(roleRows(screen.getByRole("region", { name: "multi options" }))[1]).toEqual([
      "User prompt",
      "urgencyNot set. Every LLM step needs a user prompt.",
    ]);
  });

  it("shows both roles even when the catalog tiers them advanced", () => {
    usePluginCatalogStore.setState({
      key: "alice:fp-1",
      principal: "alice",
      fingerprint: "fp-1",
      schemas: {
        "transform:llm": {
          name: "llm",
          plugin_type: "transform",
          description: "",
          json_schema: {},
          knob_schema: { fields: [{ name: "system_prompt", tier: "advanced" }, { name: "prompt_template", tier: "advanced" }] },
        },
      },
    } as never);
    render(
      <OptionRows options={{ prompt_template: "Classify {{ row.text }}", system_prompt: "You classify tickets." }} ariaLabel="arm options" plugin={LLM} />,
    );
    const region = screen.getByRole("region", { name: "arm options" });
    expect(roleRows(region)).toHaveLength(2);
    expect(within(region).queryByText(/Advanced settings/)).not.toBeInTheDocument();
  });

  it("adds no prompt rows to a step that is not an llm transform", () => {
    render(<OptionRows options={{ mapping: { id: "id" } }} ariaLabel="tidy options" plugin={{ kind: "transform", name: "field_mapper" }} />);
    const region = screen.getByRole("region", { name: "tidy options" });
    expect(region.querySelector(".option-rows-prompt-roles")).toBeNull();
    expect(region.textContent).not.toMatch(/System prompt|User prompt/);
  });
});
