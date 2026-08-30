import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { pluginDisplayName } from "@/components/catalog/pluginDisplayName";
import { useSessionStore } from "@/stores/sessionStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";
import { resetStore } from "@/test/store-helpers";
import { makeComposition } from "@/test/composerFixtures";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import { PipelineSpecView } from "./PipelineSpecView";

describe("PipelineSpecView", () => {
  beforeEach(() => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: null,
    });
    resetStore(usePreferencesStore);
    // OptionRows (rendered per row in the Spec tab) now reads the catalog
    // store's schema cache; reset it so no test's seeded schema leaks into a
    // later one.
    resetStore(usePluginCatalogStore);
  });

  it("renders current metadata and the existing plain-language gloss first", () => {
    useSessionStore.setState({
      compositionState: makeComposition(7, {
        metadata: {
          name: "Customer intake",
          description: "Normalise and retain submitted customer records.",
        },
      }),
    });

    const { container } = render(<PipelineSpecView />);

    expect(
      screen.getByRole("heading", { name: "Customer intake", level: 2 }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Normalise and retain submitted customer records."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("pipeline-gloss")).toBeInTheDocument();
    const heading = screen.getByRole("heading", {
      name: "Customer intake",
      level: 2,
    });
    const gloss = screen.getByTestId("pipeline-gloss");
    const sources = screen.getByRole("region", { name: "Sources" });
    expect(
      heading.compareDocumentPosition(gloss) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(
      gloss.compareDocumentPosition(sources) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(container).toHaveTextContent("Sources");
    expect(container).toHaveTextContent("Nodes");
    expect(container).toHaveTextContent("Outputs");
  });

  it("names a plugin in the human register and keeps the raw id in title", () => {
    // elspeth-ca456d9d8d: the Plugin row printed the raw catalog id as visible
    // text; the copy register puts the display name on the surface and the
    // exact id in `title`, where operators can still read and copy it.
    useSessionStore.setState({ compositionState: makeComposition(1) });

    render(<PipelineSpecView />);

    const sources = screen.getByRole("region", { name: "Sources" });
    const pluginValue = within(sources).getByText(pluginDisplayName("csv_file"));
    expect(pluginValue.tagName).toBe("DD");
    expect(pluginValue).toHaveAttribute("title", "csv_file");
    expect(within(sources).queryByText("csv_file")).toBeNull();
  });

  it("uses deterministic source ordering while preserving node and output order", () => {
    useSessionStore.setState({
      compositionState: makeComposition(3, {
        sources: {
          zebra: { plugin: "z_source", options: {} },
          alpha: { plugin: "a_source", options: {} },
        },
        nodes: [
          {
            id: "node-second",
            node_type: "gate",
            plugin: "expression",
            input: "zebra",
            on_success: null,
            on_error: null,
            options: {},
          },
          {
            id: "node-first",
            node_type: "transform",
            plugin: "passthrough",
            input: "alpha",
            on_success: null,
            on_error: null,
            options: {},
          },
        ],
        outputs: [
          { name: "output-second", plugin: "json", options: {} },
          { name: "output-first", plugin: "csv", options: {} },
        ],
      }),
    });

    render(<PipelineSpecView />);

    expect(
      within(screen.getByRole("region", { name: "Sources" }))
        .getAllByRole("article")
        .map((card) => card.getAttribute("aria-label")),
    ).toEqual(["Source alpha", "Source zebra"]);
    expect(
      within(screen.getByRole("region", { name: "Nodes" }))
        .getAllByRole("article")
        .map((card) => card.getAttribute("aria-label")),
    ).toEqual(["Node node-second", "Node node-first"]);
    expect(
      within(screen.getByRole("region", { name: "Outputs" }))
        .getAllByRole("article")
        .map((card) => card.getAttribute("aria-label")),
    ).toEqual(["Output output-second", "Output output-first"]);
  });

  it("shows only non-null authoritative routing fields and omits unrelated state", () => {
    useSessionStore.setState({
      compositionState: makeComposition(4, {
        sources: {
          input: {
            plugin: "csv",
            options: {},
            on_success: "rows",
          },
        },
        nodes: [
          {
            id: "rows",
            node_type: "gate",
            plugin: "expression",
            input: "input",
            on_success: "accepted",
            on_error: null,
            routes: { rejected: "quarantine" },
            fork_to: null,
            condition: "DO_NOT_DERIVE_PRIVATE_GRAPH_CONFIG",
            options: {},
          },
        ],
        outputs: [
          {
            name: "accepted",
            plugin: "json",
            on_write_failure: "discard",
            options: {},
          },
        ],
        edges: [
          {
            id: "DO_NOT_RENDER_EDGE",
            from_node: "input",
            to_node: "rows",
            edge_type: "on_success",
            label: "DO_NOT_RENDER_EDGE",
          },
        ],
        validation_errors: ["DO_NOT_RENDER_VALIDATION"],
      }),
    });

    render(<PipelineSpecView />);

    const source = screen.getByRole("article", { name: "Source input" });
    expect(within(source).getByText("Then")).toBeInTheDocument();
    expect(within(source).queryByText("Rows failing validation")).not.toBeInTheDocument();
    const node = screen.getByRole("article", { name: "Node rows" });
    expect(within(node).getByText("Reads from")).toBeInTheDocument();
    expect(within(node).getByText("Then")).toBeInTheDocument();
    expect(within(node).getByText("Routes")).toBeInTheDocument();
    expect(within(node).queryByText("On error")).not.toBeInTheDocument();
    expect(within(node).queryByText("Forks every row to")).not.toBeInTheDocument();
    expect(node).not.toHaveTextContent("DO_NOT_DERIVE_PRIVATE_GRAPH_CONFIG");
    const output = screen.getByRole("article", { name: "Output accepted" });
    expect(within(output).getByText("If writing fails")).toBeInTheDocument();
    expect(screen.queryByText("DO_NOT_RENDER_EDGE")).not.toBeInTheDocument();
    expect(screen.queryByText("DO_NOT_RENDER_VALIDATION")).not.toBeInTheDocument();
  });

  it("projects a coalesce's fan-in config — branches, policy and merge", () => {
    // Regression for elspeth-59684fb0c8. nodeRows() mapped only `routes`
    // and `fork_to`, so a coalesce rendered as `input: pairing_done` and
    // nothing else. `input` on a fan-in node is just the backend-compatible
    // FIRST-BRANCH PLACEHOLDER, so showing it alone is actively misleading:
    // an operator checking the Spec tab because the graph looked wrong
    // (elspeth-625e85c59b) had the wrong topology confirmed rather than
    // corrected. Live shape: session 75cec2b2 v22.
    useSessionStore.setState({
      compositionState: makeComposition(5, {
        sources: {
          source: { plugin: "csv", options: {}, on_success: "colours_raw" },
        },
        nodes: [
          {
            id: "merge_branches",
            node_type: "coalesce",
            plugin: null,
            input: "pairing_done",
            on_success: "final_out",
            on_error: null,
            branches: { branch_a: "pairing_done", branch_b: "hex_done" },
            policy: "require_all",
            merge: "union",
            options: {},
          },
        ],
        outputs: [
          {
            name: "final_out",
            plugin: "csv",
            on_write_failure: "discard",
            options: {},
          },
        ],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node merge_branches" });
    expect(within(node).getByText("Merges branches")).toBeInTheDocument();
    // Both aliases, including the one `input` does not name. Neither
    // connection has a producer in this fixture — its only source publishes
    // `colours_raw` and the coalesce publishes `final_out` — so the upstream
    // lookup falls through to titleCaseLabel, which is the correct reading of
    // a fixture whose upstream arms are not modelled.
    expect(node).toHaveTextContent("Branch A → Pairing Done");
    expect(node).toHaveTextContent("Branch B → Hex Done");
    expect(within(node).getByText("Merge policy")).toBeInTheDocument();
    expect(node).toHaveTextContent("wait for every branch");
    expect(within(node).getByText("wait for every branch")).toHaveAttribute(
      "title",
      "require_all",
    );
    // Exact match: "Merge" is a prefix of "Merges branches" and "Merge
    // policy", both present on this same card, so a substring check here
    // would pass regardless of whether this row rendered at all.
    expect(within(node).getByText("Merge")).toBeInTheDocument();
    expect(node).toHaveTextContent("combine every branch's fields");
  });

  // Live-check finding (session 39578c6f, Spec tab, show_advanced off):
  // `routingValue()` humanised the `routes` alias→target map into prose but
  // not the structurally identical `branches` map, so a coalesce's fan-in
  // map rendered as a raw `{"branch_invest_cs1":"invest_cs1_done",...}`
  // JSON string in a plain <dd> — not wrapped in <code>/<details>.
  it("renders a coalesce's branch map as prose, never a raw JSON string", () => {
    useSessionStore.setState({
      compositionState: makeComposition(8, {
        sources: {
          source: { plugin: "csv", options: {}, on_success: "docs" },
        },
        nodes: [
          {
            id: "merge_invest",
            node_type: "coalesce",
            plugin: null,
            input: "assess_invest_cs1_done",
            on_success: "tidy_output",
            on_error: null,
            branches: {
              branch_invest_cs1: "invest_cs1_done",
              branch_invest_cs2: "invest_cs2_done",
            },
            policy: "require_all",
            merge: "union",
            options: {},
          },
        ],
        outputs: [
          {
            name: "tidy_output",
            plugin: "csv",
            on_write_failure: "discard",
            options: {},
          },
        ],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node merge_invest" });
    expect(node).not.toHaveTextContent('{"');
    expect(node).toHaveTextContent(
      "Branch Invest Cs1 → Invest Cs1 Done; Branch Invest Cs2 → Invest Cs2 Done",
    );
    // The raw map stays one hover away.
    expect(within(node).getByText(/^Branch Invest Cs1/)).toHaveAttribute(
      "title",
      "branch_invest_cs1 → invest_cs1_done; branch_invest_cs2 → invest_cs2_done",
    );
  });

  it("projects a collector's scope binding — which group it closes, under which policy", () => {
    // Parity gap found by adversarial review of the coalesce fix: that fix
    // closed the fan-in arm of a defect it had itself diagnosed as general,
    // and walked past the collector. A collector's `input` names its
    // connection but says nothing about the scope BINDING — which expand
    // group it closes (scope_opener / scope_name) and what arrival policy
    // governs it (scope_policy). All 66 collectors in the saved corpus
    // populate all three, and COLLECTOR_PHRASE in the prose gloss is a fixed
    // string that names none of them, so before this the operator had no
    // surface at all for a collector's wiring.
    //
    // scope_policy in particular must be visible: composer/state.py declines
    // to default it precisely because require_all and best_effort "look
    // identical and the remedies are inverted".
    useSessionStore.setState({
      compositionState: makeComposition(6, {
        sources: {
          source: { plugin: "csv", options: {}, on_success: "docs" },
        },
        nodes: [
          // The opener the collector's scope binds to. Before Wave 3 this
          // fixture named `explode_pages` in scope_opener without the node
          // existing anywhere in the composition; scope_opener is the one
          // routing field that resolves a COMPONENT, so an absent opener
          // renders "Removed" — which is not what this test is about.
          {
            id: "explode_pages",
            node_type: "transform",
            plugin: "json_explode",
            input: "docs",
            on_success: "pages",
            on_error: null,
            options: {},
          },
          {
            id: "gather_pages",
            node_type: "collector",
            plugin: "batch_llm",
            input: "pages",
            on_success: "final_out",
            on_error: null,
            scope_name: "doc_pages",
            scope_opener: "explode_pages",
            scope_policy: "require_all",
            output_mode: "passthrough",
            timeout_seconds: 300,
            options: {},
          },
        ],
        outputs: [
          {
            name: "final_out",
            plugin: "csv",
            on_write_failure: "discard",
            options: {},
          },
        ],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node gather_pages" });
    // Exact match: "Scope" is a prefix of "Scope opened by" and "Scope
    // policy", both present on this same card.
    expect(within(node).getByText("Scope")).toBeInTheDocument();
    // A scope NAME is neither a connection nor an enum, so it renders through
    // the author-name rule: title-cased, raw in `title`.
    const scopeName = within(node).getByText("Scope").nextElementSibling;
    expect(scopeName).toHaveTextContent("Doc Pages");
    expect(scopeName).toHaveAttribute("title", "doc_pages");
    expect(within(node).getByText("Scope opened by")).toBeInTheDocument();
    const opener = within(node).getByText("Scope opened by").nextElementSibling;
    expect(opener).toHaveTextContent("Explode Pages");
    expect(opener).toHaveAttribute("title", "explode_pages");
    expect(within(node).getByText("Scope policy")).toBeInTheDocument();
    expect(node).toHaveTextContent("wait for every row in the group");
    expect(node).toHaveTextContent("Output mode");
    expect(node).toHaveTextContent("pass rows through unchanged");
    expect(node).toHaveTextContent("Waits up to (seconds)");
    expect(node).toHaveTextContent("300");
  });

  it("says Removed for a collector whose scope opener was deleted (elspeth-93f5621f18)", () => {
    // The other arm of the scope_opener ruling: every OTHER routing value
    // names a connection, where a missing far end is dangling rather than
    // removed. scope_opener names a COMPONENT, so a deleted opener is the
    // one place "Removed" is the honest word.
    useSessionStore.setState({
      compositionState: makeComposition(14, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          {
            id: "gather_pages",
            node_type: "collector",
            plugin: null,
            input: "raw_rows",
            on_success: "final_out",
            on_error: null,
            scope_name: "doc_pages",
            scope_opener: "explode_pages",
            scope_policy: "require_all",
            output_mode: "passthrough",
            timeout_seconds: 300,
            options: {},
          },
        ],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node gather_pages" });
    const dd = within(node).getByText("Scope opened by").nextElementSibling;
    expect(dd).toHaveTextContent("Removed");
    expect(dd).toHaveAttribute("title", "explode_pages");
  });

  it("omits branches, policy and merge on nodes that carry none", () => {
    // The routing block filters nulls, so widening it must not add empty
    // fan-in rows to every transform card.
    useSessionStore.setState({
      compositionState: makeComposition(2, {
        sources: {
          source: { plugin: "csv", options: {}, on_success: "rows" },
        },
        nodes: [
          {
            id: "enrich",
            node_type: "transform",
            plugin: "llm",
            input: "rows",
            on_success: "done",
            on_error: null,
            options: {},
          },
        ],
        outputs: [],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node enrich" });
    expect(node).toHaveTextContent("Reads from");
    expect(node).not.toHaveTextContent("Merges branches");
    expect(node).not.toHaveTextContent("Merge policy");
    expect(node).not.toHaveTextContent("Merge");
  });

  // The always-visible pretty-printed JSON dump this test pinned
  // ("formats options deterministically in focusable labelled code
  // regions") is deliberately removed: options now render through the
  // shared OptionRows (elspeth-b9ebdf9011), whose own suite
  // (OptionRows.test.tsx) covers the essential/advanced split and the
  // show_advanced-gated raw-JSON block that supersede it. The removed
  // test also pinned `tabIndex=0` on the options wrapper, which made
  // sense for a scrollable code block but not for structured dl rows —
  // OptionRows renders `role="region"` with no tabIndex, and this file's
  // new "renders options through OptionRows..." test below covers the
  // Spec tab's integration with it.

  it("keeps each dt/dd pair wrapped in a div so the spec-card grid contract holds", () => {
    // .pipeline-spec-card dl lays out via grid + display:contents on the
    // direct <div> children — the markup shape IS the styling contract.
    useSessionStore.setState({
      compositionState: makeComposition(2, {
        sources: {
          input: {
            plugin: "csv",
            options: {},
            on_success: "rows",
          },
        },
        nodes: [],
        outputs: [],
      }),
    });

    render(<PipelineSpecView />);

    const card = screen.getByRole("article", { name: "Source input" });
    const definitionList = card.querySelector("dl");
    expect(definitionList).not.toBeNull();
    const rows = Array.from(definitionList?.children ?? []);
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.tagName).toBe("DIV");
      expect(
        Array.from(row.children).map((child) => child.tagName),
      ).toEqual(["DT", "DD"]);
    }
  });

  it("reports empty sections without inventing components", () => {
    useSessionStore.setState({
      compositionState: makeComposition(1, {
        sources: {},
        nodes: [],
        outputs: [],
      }),
    });

    render(<PipelineSpecView />);

    expect(screen.getByRole("region", { name: "Sources" })).toHaveTextContent(
      "No sources.",
    );
    expect(screen.getByRole("region", { name: "Nodes" })).toHaveTextContent(
      "No nodes.",
    );
    expect(screen.getByRole("region", { name: "Outputs" })).toHaveTextContent(
      "No outputs.",
    );
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("renders composer-authored step descriptions as prose above the config and omits the node when absent or blank", () => {
    useSessionStore.setState({
      compositionState: makeComposition(4, {
        sources: {
          source: {
            plugin: "csv_file",
            options: { path: "x.csv" },
            description: "Read the three project-brief pages.",
          },
        },
        nodes: [
          {
            id: "summarize_page",
            node_type: "transform",
            plugin: "llm",
            input: "source",
            on_success: null,
            on_error: null,
            options: {},
            description: "Have an LLM write a short summary of each page.",
          },
          {
            id: "undescribed_node",
            node_type: "transform",
            plugin: "passthrough",
            input: "source",
            on_success: null,
            on_error: null,
            options: {},
            description: "   ",
          },
        ],
        outputs: [
          {
            name: "results",
            plugin: "json",
            options: {},
            description: "Write url and summary to a JSON file.",
          },
        ],
      }),
    });

    render(<PipelineSpecView />);

    const sourceCard = screen.getByRole("article", { name: "Source source" });
    const sourceProse = within(sourceCard).getByText(
      "Read the three project-brief pages.",
    );
    expect(sourceProse).toHaveClass("pipeline-spec-step-description");
    // Prose sits between the card caption and the config grid.
    const sourceHeading = within(sourceCard).getByRole("heading", { level: 4 });
    expect(
      sourceHeading.compareDocumentPosition(sourceProse) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);

    const nodeCard = screen.getByRole("article", { name: "Node summarize_page" });
    expect(
      within(nodeCard).getByText(
        "Have an LLM write a short summary of each page.",
      ),
    ).toBeInTheDocument();

    const outputCard = screen.getByRole("article", { name: "Output results" });
    expect(
      within(outputCard).getByText("Write url and summary to a JSON file."),
    ).toBeInTheDocument();

    // A missing or whitespace-only description renders no prose node at all.
    const undescribedCard = screen.getByRole("article", {
      name: "Node undescribed_node",
    });
    expect(
      undescribedCard.querySelector(".pipeline-spec-step-description"),
    ).toBeNull();
  });

  it("renders options through OptionRows and never shows hashes or blob refs by default", () => {
    useSessionStore.setState({
      compositionState: makeComposition(7, {
        sources: {
          source: {
            plugin: "csv",
            on_success: "raw_rows",
            on_validation_failure: "discard",
            options: {
              path: "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
              blob_ref: "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
              interpretation_requirements: [{ accepted_artifact_hash: "3".repeat(64) }],
              schema: { mode: "observed" },
            },
          },
        },
      }),
    });
    render(<PipelineSpecView />);
    const card = screen.getByRole("article", { name: "Source source" });
    expect(within(card).getByRole("region", { name: "Source source settings" })).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/f976fd8b-4432/);
    expect(card.textContent).not.toMatch(/3{64}/);
    expect(within(card).queryByText("None")).not.toBeInTheDocument();
    expect(within(card).getByText("Rows failing validation")).toBeInTheDocument();
    expect(within(card).getByText("dropped (recorded in the audit trail)")).toBeInTheDocument();
  });

  // Round-3 review finding, Spec-tab side: OptionRows' nested-key
  // humanising must fail closed on `field_mapping` (a reader-keyed
  // column→target map, not ELSPETH's own schema vocabulary) through THIS
  // integration path too, not only in OptionRows' own unit tests.
  it("renders node ids and kinds in the reader register with the raw id in title (elspeth-93f5621f18 / elspeth-d74ab492dd)", () => {
    useSessionStore.setState({
      compositionState: makeComposition(10, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "extract_invoice", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "results", on_error: null, options: {} },
          { id: "split_rows", node_type: "row_union", plugin: null, input: "results", on_success: "results", on_error: null, options: {} },
        ],
        outputs: [{ name: "results", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node extract_invoice" });
    expect(within(node).getByRole("heading", { level: 4 })).toHaveTextContent("Extract Invoice");
    expect(within(node).getByRole("heading", { level: 4 })).toHaveAttribute("title", "extract_invoice");
    expect(within(node).getByText("Reads from").nextElementSibling).toHaveTextContent("Source");
    expect(within(node).getByText("Then").nextElementSibling).toHaveTextContent("Results");
    expect(within(node).getByText("Then").nextElementSibling).toHaveAttribute("title", "results");
    const union = screen.getByRole("article", { name: "Node split_rows" });
    expect(within(union).getByText("Kind").nextElementSibling).toHaveTextContent("Row Union");
  });

  it("names the component feeding each branch of a wired coalesce (elspeth-93f5621f18)", () => {
    // The direction pin. A fan-in node's own `input` is one of its own branch
    // connections, so resolving `branches` through consumers rather than
    // producers renders "Branch Invest Cs1 → Merge Invest" — the node naming
    // itself. This fixture WIRES the upstream arm so the two directions give
    // visibly different answers and the wrong one cannot pass.
    useSessionStore.setState({
      compositionState: makeComposition(13, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "extract_invoice", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "invest_cs1_done", on_error: null, options: {} },
          { id: "merge_invest", node_type: "coalesce", plugin: null, input: "invest_cs1_done", on_success: "tidy_output", on_error: null, branches: { branch_invest_cs1: "invest_cs1_done", branch_invest_cs2: "invest_cs2_done" }, policy: "require_all", merge: "union", options: {} },
        ],
        outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node merge_invest" });
    expect(within(node).getByText("Merges branches").nextElementSibling).toHaveTextContent(
      "Branch Invest Cs1 → Extract Invoice; Branch Invest Cs2 → Invest Cs2 Done",
    );
    expect(node).not.toHaveTextContent("Branch Invest Cs1 → Merge Invest");
  });

  it("does not repeat a structural node's description as its heading (elspeth-93f5621f18)", () => {
    // componentPhrase resolves a plugin-less node to
    // descriptionLabel(node.description), and the card already renders that
    // description as the paragraph directly under the heading. Using
    // componentPhrase for the <h4> therefore prints the same sentence twice
    // and leaves the card unnamed.
    useSessionStore.setState({
      compositionState: makeComposition(15, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [
          { id: "merge_assessments", node_type: "coalesce", plugin: null, input: "raw_rows",
            on_success: "final_out", on_error: null,
            description: "Merge the two assessment branches",
            branches: { branch_a: "raw_rows", branch_b: "second_pass" },
            policy: "require_all", merge: "union", options: {} },
        ],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
      }),
    });

    render(<PipelineSpecView />);

    const node = screen.getByRole("article", { name: "Node merge_assessments" });
    const heading = within(node).getByRole("heading", { level: 4 });
    expect(heading).toHaveTextContent("Merge Assessments");
    expect(heading).toHaveAttribute("title", "merge_assessments");
    // The description keeps its own slot and is NOT the heading.
    expect(within(node).getByText("Merge the two assessment branches")).not.toBe(heading);
  });

  it("default DOM of the Spec tab passes the shared identifier pin (card names exempted by design)", () => {
    useSessionStore.setState({
      compositionState: makeComposition(11, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" } },
        nodes: [
          // The digit-free constraint on these ids is RETIRED (2026-08-31):
          // SNAKE_RE now admits digits, so `invest_cs1_done` would produce a
          // pin that fails honestly. The ids are left as they are — renaming
          // them would churn the assertions below without testing anything
          // new; the pin's reach no longer depends on the spelling.
          { id: "merge_invest", node_type: "coalesce", plugin: null, input: "invest_first_done", on_success: "tidy_output", on_error: null, branches: { branch_invest_first: "invest_first_done" }, policy: "require_all", merge: "union", options: {} },
          { id: "collect_pages", node_type: "collector", plugin: null, input: "tidy_output", on_success: "tidy_output", on_error: null, scope_name: "doc_pages", scope_opener: "merge_invest", scope_policy: "require_all", output_mode: "passthrough", timeout_seconds: 300, options: {} },
        ],
        outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
      }),
    });

    const { container } = render(<PipelineSpecView />);

    expectNoIdentifiersInDefaultDom(container, {
      // SELF-only: both labels are ON these elements, so a control added
      // inside a spec card later is still scanned. `closest()` would exempt
      // the aria half of the pin for the whole Spec tab, permanently.
      allowAriaLabelSelfSelectors: ["article.pipeline-spec-card", "div.option-rows"],
    });
  });

  it("renders field_mapping's reader-authored keys verbatim through the Spec tab", () => {
    useSessionStore.setState({
      compositionState: makeComposition(9, {
        sources: {
          source: {
            plugin: "csv",
            on_success: "rows",
            options: {
              field_mapping: { weird_header: "b" },
            },
          },
        },
      }),
    });
    render(<PipelineSpecView />);
    const card = screen.getByRole("article", { name: "Source source" });
    expect(within(card).getByText("weird_header")).toBeInTheDocument();
    expect(within(card).queryByText("Weird Header")).not.toBeInTheDocument();
    expect(card.textContent).not.toMatch(/Weird Header/);
  });
});
