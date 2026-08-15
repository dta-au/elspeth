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
