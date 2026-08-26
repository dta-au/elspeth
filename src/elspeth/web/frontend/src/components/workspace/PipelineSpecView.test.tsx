import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useSessionStore } from "@/stores/sessionStore";
import { makeComposition } from "@/test/composerFixtures";
import { PipelineSpecView } from "./PipelineSpecView";

describe("PipelineSpecView", () => {
  beforeEach(() => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: null,
    });
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
    expect(source).toHaveTextContent("on_success");
    expect(source).not.toHaveTextContent("on_validation_failure");
    const node = screen.getByRole("article", { name: "Node rows" });
    expect(node).toHaveTextContent("input");
    expect(node).toHaveTextContent("on_success");
    expect(node).toHaveTextContent("routes");
    expect(node).not.toHaveTextContent("on_error");
    expect(node).not.toHaveTextContent("fork_to");
    expect(node).not.toHaveTextContent("DO_NOT_DERIVE_PRIVATE_GRAPH_CONFIG");
    const output = screen.getByRole("article", { name: "Output accepted" });
    expect(output).toHaveTextContent("on_write_failure");
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
    expect(node).toHaveTextContent("branches");
    // Both aliases, including the one `input` does not name.
    expect(node).toHaveTextContent("branch_a");
    expect(node).toHaveTextContent("hex_done");
    expect(node).toHaveTextContent("policy");
    expect(node).toHaveTextContent("require_all");
    expect(node).toHaveTextContent("merge");
    expect(node).toHaveTextContent("union");
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
    expect(node).toHaveTextContent("scope_name");
    expect(node).toHaveTextContent("doc_pages");
    expect(node).toHaveTextContent("scope_opener");
    expect(node).toHaveTextContent("explode_pages");
    expect(node).toHaveTextContent("scope_policy");
    expect(node).toHaveTextContent("require_all");
    expect(node).toHaveTextContent("output_mode");
    expect(node).toHaveTextContent("passthrough");
    expect(node).toHaveTextContent("timeout_seconds");
    expect(node).toHaveTextContent("300");
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
    expect(node).toHaveTextContent("input");
    expect(node).not.toHaveTextContent("branches");
    expect(node).not.toHaveTextContent("policy");
    expect(node).not.toHaveTextContent("merge");
  });

  it("formats options deterministically in focusable labelled code regions", () => {
    useSessionStore.setState({
      compositionState: makeComposition(2, {
        sources: {
          input: {
            plugin: "csv",
            options: { delimiter: ",", header: true },
          },
        },
        nodes: [],
        outputs: [],
      }),
    });

    render(<PipelineSpecView />);

    const options = screen.getByRole("region", {
      name: "Source input options",
    });
    expect(options).toHaveAttribute("tabindex", "0");
    // Prove the highlighted JSON path rendered, not the plain fallback.
    const highlighted = options.querySelector("[data-codeblock-format='json']");
    expect(highlighted).not.toBeNull();
    expect(
      options.querySelector("[data-codeblock-format='plain']"),
    ).toBeNull();
    // Byte-exact oracle, indentation included. The shared prism highlighter
    // renders one <div> per line, so the container's textContent loses the
    // newlines with no separator — reconstruct them from the per-line
    // children rather than normalising whitespace away. Deleting the
    // whitespace would leave 2-space pretty-printing unpinned, and
    // pretty-printing is the whole reason the Spec tab routes through
    // CodeBlock with prettyJson.
    const renderedLines = Array.from(highlighted?.children ?? []).map(
      (line) => line.textContent ?? "",
    );
    expect(renderedLines.join("\n")).toBe(
      JSON.stringify({ delimiter: ",", header: true }, null, 2),
    );
  });

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
});
