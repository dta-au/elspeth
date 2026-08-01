import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { WireStageData } from "@/types/guided";
import { buildEntityNames, reconstructWireEdges, WireStageTurn } from "./WireStageTurn";

const SOURCE_ID = "00000000-0000-4000-8000-000000000010";
const NODE_ID = "00000000-0000-4000-8000-000000000020";
const OUTPUT_ID = "00000000-0000-4000-8000-000000000030";
const EDGE_ID = "00000000-0000-4000-8000-000000000040";

function canonicalData(overrides: Partial<WireStageData> = {}): WireStageData {
  return {
    proposal_id: "00000000-0000-4000-8000-000000000001",
    draft_hash: "d".repeat(64),
    sources: [{
      stable_id: SOURCE_ID,
      label: "source-1",
      plugin: "inline_blob",
      on_validation_failure: "discard",
      guaranteed_fields: ["body"],
      row_cardinality: { input: "none", output: "zero_or_many", expected_output_count: null },
    }],
    nodes: [{
      stable_id: NODE_ID,
      label: "node-1",
      node_type: "transform",
      plugin: "field_mapper",
      behavior: { kind: "transform" },
      required_fields: ["body"],
      guaranteed_fields: ["mapped"],
      row_cardinality: { input: "one", output: "one", expected_output_count: null },
      structured_output_fields: [],
      node_options_summary: [],
    }],
    outputs: [{
      stable_id: OUTPUT_ID,
      label: "output-1",
      plugin: "json",
      on_write_failure: "discard",
      required_fields: ["mapped"],
      business_schema: { mode: "observed", fields: [], guaranteed_fields: [], required_fields: ["mapped"] },
    }],
    connections: [{
      stable_id: EDGE_ID,
      from_endpoint: { kind: "source", stable_id: SOURCE_ID },
      to_endpoint: { kind: "node", stable_id: NODE_ID },
      flow: { kind: "source_success", branch: null },
      schema_contract: {
        from: "source",
        to: "mapper",
        producer_guarantees: ["body"],
        consumer_requires: ["body"],
        missing_fields: [],
        satisfied: true,
      },
    }, {
      stable_id: "00000000-0000-4000-8000-000000000041",
      from_endpoint: { kind: "node", stable_id: NODE_ID },
      to_endpoint: { kind: "output", stable_id: OUTPUT_ID },
      flow: { kind: "node_success", branch: null },
      schema_contract: null,
    }],
    semantic_contracts: [],
    warnings: [],
    blockers: [],
    can_confirm: true,
    ...overrides,
  };
}

describe("WireStageTurn", () => {
  it("renders candidate-authored connections without reconstructing a spine", () => {
    const data = canonicalData();
    expect(reconstructWireEdges(data)).toEqual([
      expect.objectContaining({ from: SOURCE_ID, to: NODE_ID, label: "source_success", satisfied: true }),
      expect.objectContaining({ from: NODE_ID, to: OUTPUT_ID, label: "node_success", satisfied: null }),
    ]);
    expect(buildEntityNames(data)).toEqual(new Map([
      [SOURCE_ID, "source-1"],
      [NODE_ID, "node-1 (Output)"],
      [OUTPUT_ID, "output-1"],
      ["discard", "Discard"],
    ]));
  });

  it("confirms only when server-authored blockers permit it", async () => {
    const onConfirm = vi.fn();
    const { rerender } = render(<WireStageTurn data={canonicalData()} onConfirm={onConfirm} confirmDisabled={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Confirm wiring" }));
    expect(onConfirm).toHaveBeenCalledOnce();

    rerender(<WireStageTurn data={canonicalData({ blockers: [{ message: "invalid route" }], can_confirm: false })} onConfirm={onConfirm} confirmDisabled={false} />);
    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeDisabled();
    expect(screen.getByText("invalid route")).toBeInTheDocument();
  });

  it("submits bounded correction feedback against the selected stable target", async () => {
    const onCorrect = vi.fn();
    render(<WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} onCorrect={onCorrect} />);
    await userEvent.selectOptions(screen.getByLabelText("Component"), NODE_ID);
    await userEvent.type(screen.getByLabelText("What should change?"), "Add the reviewed mapping.");
    await userEvent.click(screen.getByRole("button", { name: "Re-plan wiring" }));
    expect(onCorrect).toHaveBeenCalledWith({ kind: "node", stable_id: NODE_ID }, "Add the reviewed mapping.");
  });

  it("summarises route status once and renders per-route chips, not trailing prose", () => {
    // canonicalData: one satisfied contract (connected) + one null contract
    // (not yet checked). The per-line "— not yet checked" dangling clause was
    // the operator-reported debug-dump read; status renders as a compact chip
    // with the count summary above the list.
    render(<WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />);

    expect(screen.getByText("2 routes — 1 connected, 1 not yet checked")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("not yet checked")).toBeInTheDocument();
    // Screen readers keep the status even though it left the visible prose:
    // the row's accessible name carries it (aria-label overrides li content).
    const sourceRoute = screen.getByRole(
      "listitem",
      { name: "source-1 to node-1 (Output) — Source success — connected" },
    );
    expect(sourceRoute).toHaveAttribute("data-edge-id", EDGE_ID);
    expect(
      screen.getByRole(
        "listitem",
        { name: "node-1 (Output) to output-1 — Node success — not yet checked" },
      ),
    ).toBeInTheDocument();
  });

  it("keeps parallel gate forks uniquely identifiable in route and correction controls", async () => {
    const user = userEvent.setup();
    const onCorrect = vi.fn();
    const gateId = "00000000-0000-4000-8000-000000000024";
    const rowUnionId = "00000000-0000-4000-8000-000000000025";
    const controlEdgeId = "00000000-0000-4000-8000-000000000050";
    const treatmentEdgeId = "00000000-0000-4000-8000-000000000051";
    const nodes: WireStageData["nodes"] = [
      {
        stable_id: gateId,
        label: "triage",
        node_type: "gate",
        plugin: null,
        behavior: {
          kind: "gate",
          condition: "row['variant']",
          route_aliases: ["route-1", "route-2"],
          routes: [
            { alias: "route-1", key: "control" },
            { alias: "route-2", key: "treatment" },
          ],
          fork_branches: [
            { routes: ["route-1"], branch: "branch-1" },
            { routes: ["route-2"], branch: "branch-2" },
          ],
        },
        node_options_summary: [],
        required_fields: ["variant"],
        guaranteed_fields: [],
        row_cardinality: { input: "one", output: "one", expected_output_count: null },
        structured_output_fields: [],
      },
      {
        stable_id: rowUnionId,
        label: "variant union",
        node_type: "row_union",
        plugin: null,
        behavior: {
          kind: "row_union",
          branch_aliases: ["branch-1", "branch-2"],
          policy: "require_all",
          timeout_seconds: null,
        },
        node_options_summary: [],
        required_fields: [],
        guaranteed_fields: ["variant"],
        row_cardinality: {
          input: "branches",
          output: "one_per_branch",
          expected_output_count: null,
        },
        structured_output_fields: [],
      },
    ];
    const connections: WireStageData["connections"] = [
      {
        stable_id: controlEdgeId,
        from_endpoint: { kind: "node", stable_id: gateId },
        to_endpoint: { kind: "node", stable_id: rowUnionId },
        flow: { kind: "gate_fork", routes: ["route-1"], branch: "branch-1" },
        schema_contract: null,
      },
      {
        stable_id: treatmentEdgeId,
        from_endpoint: { kind: "node", stable_id: gateId },
        to_endpoint: { kind: "node", stable_id: rowUnionId },
        flow: { kind: "gate_fork", routes: ["route-2"], branch: "branch-2" },
        schema_contract: null,
      },
    ];

    render(
      <WireStageTurn
        data={canonicalData({ nodes, connections })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
        onCorrect={onCorrect}
      />,
    );

    const rows = Array.from(
      screen.getByRole("list", { name: "Wiring routes" }).querySelectorAll(":scope > li"),
    );
    const identities = rows.map((row) => row.getAttribute("data-edge-id"));
    expect({
      identities,
      uniqueIdentityCount: new Set(identities).size,
      accessibleNames: rows.map((row) => row.getAttribute("aria-label")),
    }).toEqual({
      identities: [controlEdgeId, treatmentEdgeId],
      uniqueIdentityCount: 2,
      accessibleNames: [
        "triage (Gate) to variant union (Row Union) — Gate fork route-1 (when control) as branch-1 — not yet checked",
        "triage (Gate) to variant union (Row Union) — Gate fork route-2 (when treatment) as branch-2 — not yet checked",
      ],
    });

    const controlOption = screen.getByRole("option", {
      name: "triage (Gate) → variant union (Row Union) — Gate fork route-1 (when control) as branch-1",
    });
    const treatmentOption = screen.getByRole("option", {
      name: "triage (Gate) → variant union (Row Union) — Gate fork route-2 (when treatment) as branch-2",
    });
    expect(controlOption).toHaveValue(controlEdgeId);
    expect(treatmentOption).toHaveValue(treatmentEdgeId);

    await user.selectOptions(screen.getByLabelText("Component"), treatmentEdgeId);
    await user.type(
      screen.getByLabelText("What should change?"),
      "Change only the treatment fork.",
    );
    await user.click(screen.getByRole("button", { name: "Re-plan wiring" }));
    expect(onCorrect).toHaveBeenCalledWith(
      { kind: "edge", stable_id: treatmentEdgeId },
      "Change only the treatment fork.",
    );
  });

  it("labels the correction controls and styles them as the app's form idiom", () => {
    render(<WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} onCorrect={vi.fn()} />);
    const select = screen.getByLabelText("Component");
    expect(select.tagName).toBe("SELECT");
    expect(select).toHaveClass("guided-schema-select");
    const feedback = screen.getByLabelText("What should change?");
    expect(feedback.tagName).toBe("TEXTAREA");
    // Explicit for/id association — the old wrapping-label markup overlapped
    // the bare native select with its own label text at some widths.
    expect(select).toHaveAttribute("id");
    expect(feedback).toHaveAttribute("id");
  });

  it("shows warnings, contract gaps, and technical stable ids", () => {
    const data = canonicalData({
      warnings: [{ message: "Review expansion cardinality." }],
      connections: canonicalData().connections.map((connection, index) => index === 0
        ? { ...connection, schema_contract: { ...connection.schema_contract!, missing_fields: ["body"], satisfied: false } }
        : connection),
    });
    render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    expect(screen.getByText("Review expansion cardinality.")).toBeInTheDocument();
    expect(screen.getByText("Missing fields: body")).toBeInTheDocument();
    expect(screen.getByText("Technical details")).toBeInTheDocument();
  });

  it("renders the key transform options a behavior discriminant alone cannot show", () => {
    // R2-F3: "Policy: transform each input row" was the whole story a
    // field_mapper told, so the operator could not see which fields it renames
    // or that unmapped fields are dropped.
    const data = canonicalData({
      nodes: canonicalData().nodes.map((node) => ({
        ...node,
        node_options_summary: [
          { key: "mapping", value: "given_name → first_name, meta.source → origin" },
          { key: "select_only", value: "only the mapped fields are kept" },
        ],
      })),
    });
    render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);

    expect(
      screen.getByText("Mapping: given_name → first_name, meta.source → origin"),
    ).toBeInTheDocument();
    expect(screen.getByText("Select only: only the mapped fields are kept")).toBeInTheDocument();
  });

  it("reports a missing contract without asserting why it is missing", async () => {
    // A null schema_contract is cause-free on the wire: it can mean nothing was
    // required, but equally an ADR-007 producer abstention, an error-continue
    // skip, or a discard edge. "(contract unchecked)" implied a pending check;
    // naming any single cause would be worse — it would assert a fact the
    // payload does not carry.
    const data = canonicalData({
      connections: canonicalData().connections.map((connection, index) => index === 0
        ? { ...connection, schema_contract: null }
        : connection),
    });
    render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    await userEvent.click(screen.getByText("Technical details"));

    expect(screen.queryByText(/\(contract unchecked\)/)).not.toBeInTheDocument();
    expect(
      screen.getByText(/\(contract not statically checked\)/),
    ).toBeInTheDocument();
  });

  it("never claims 'no required fields' when a sink DOES require fields and the producer abstains", async () => {
    // ADR-007 abstention (composer/state.py:2846-2874): sink_required is
    // NON-empty, but a producer with neither guarantees nor participation — a
    // select_only field_mapper on an observed schema — makes no static claim,
    // so the validator emits no EdgeContract and defers to per-row runtime
    // enforcement. The code comment there prescribes the "not yet checked"
    // reading; a row claiming "no required fields" would be flatly false.
    const base = canonicalData();
    const data = canonicalData({
      outputs: base.outputs.map((output) => ({ ...output, required_fields: ["mapped", "body"] })),
      connections: base.connections.map((connection) =>
        connection.to_endpoint.kind === "output"
          ? { ...connection, schema_contract: null }
          : connection),
    });
    render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    await userEvent.click(screen.getByText("Technical details"));

    const rawRows = screen.getByText(/00000000-0000-4000-8000-000000000041/).textContent ?? "";
    expect(rawRows).toContain("(contract not statically checked)");
    expect(rawRows).not.toContain("no required fields");
    expect(rawRows).not.toContain("not applicable");
    // The sink genuinely requires those fields — the surface must still say so.
    expect(screen.getByText("Required fields: mapped, body")).toBeInTheDocument();
    // …and the plain-language sibling keeps its own unchanged register.
    expect(screen.getAllByText(/not yet checked/).length).toBeGreaterThan(0);
  });

  it("renders the authoritative node policies, cardinality, fields, structured outputs, and business schema", () => {
    const nodes: WireStageData["nodes"] = [
      {
        ...canonicalData().nodes[0],
        plugin: "llm",
        required_fields: ["body"],
        guaranteed_fields: ["summary"],
        row_cardinality: { input: "one", output: "zero_or_many", expected_output_count: null },
        structured_output_fields: [{
          query: "classify",
          field: "classification",
          type: "str",
          enum_values: ["safe", "unsafe"],
        }],
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000021",
        label: "decision",
        node_type: "gate",
        plugin: null,
        behavior: {
          kind: "gate",
          condition: "row['classification'] == 'unsafe'",
          route_aliases: ["route-1", "route-2"],
          routes: [
            { alias: "route-1", key: "safe" },
            { alias: "route-2", key: "unsafe" },
          ],
          fork_branches: [{ routes: ["route-2"], branch: "branch-1" }],
        },
        node_options_summary: [],
        required_fields: ["classification"],
        guaranteed_fields: [],
        row_cardinality: { input: "one", output: "one", expected_output_count: null },
        structured_output_fields: [],
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000022",
        label: "batch",
        node_type: "aggregation",
        plugin: "batch_stats",
        behavior: {
          kind: "aggregation",
          trigger_kinds: ["count", "timeout"],
          count: "25",
          timeout_seconds: 12.5,
          output_mode: "transform",
          expected_output_count: "1",
        },
        node_options_summary: [],
        required_fields: ["classification"],
        guaranteed_fields: ["count"],
        row_cardinality: { input: "batch", output: "expected_count", expected_output_count: "1" },
        structured_output_fields: [],
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000023",
        label: "merge",
        node_type: "coalesce",
        plugin: null,
        behavior: {
          kind: "coalesce",
          branch_aliases: ["branch-1", "branch-2"],
          policy: "require_all",
          merge: "union",
          timeout_seconds: 7.25,
        },
        node_options_summary: [],
        required_fields: [],
        guaranteed_fields: ["count"],
        row_cardinality: { input: "branches", output: "one_per_branch_set", expected_output_count: null },
        structured_output_fields: [],
      },
    ];
    const outputs: WireStageData["outputs"] = [{
      ...canonicalData().outputs[0],
      on_write_failure: "quarantine",
      business_schema: {
        mode: "fixed",
        fields: [
          { name: "id", type: "int", required: true, nullable: false },
          { name: "email", type: "str", required: false, nullable: true },
        ],
        guaranteed_fields: ["id"],
        required_fields: ["email"],
      },
    }];

    render(<WireStageTurn data={canonicalData({ nodes, outputs })} onConfirm={vi.fn()} confirmDisabled={false} />);

    expect(screen.getByText("Cardinality: one → zero or many")).toBeInTheDocument();
    expect(screen.getAllByText("Required fields: body")).toHaveLength(1);
    expect(screen.getByText("Guaranteed fields: summary")).toBeInTheDocument();
    expect(screen.getByText("classification (str) from classify; values: safe, unsafe")).toBeInTheDocument();
    // F11: the gate details lead with the authored predicate verbatim and
    // name each author-visible route key; with no gate connection present in
    // this fixture the per-route lines carry no destination.
    expect(screen.getByText("When row['classification'] == 'unsafe'")).toBeInTheDocument();
    expect(screen.getByText("When safe (route-1)")).toBeInTheDocument();
    expect(screen.getByText("When unsafe (route-2)")).toBeInTheDocument();
    expect(screen.getByText("Routes: route-1, route-2")).toBeInTheDocument();
    expect(screen.getByText("Fork branch branch-1: route-2")).toBeInTheDocument();
    expect(screen.getByText("Triggers: count, timeout")).toBeInTheDocument();
    expect(screen.getByText("Count: 25")).toBeInTheDocument();
    expect(screen.getByText("Timeout: 12.5 seconds")).toBeInTheDocument();
    expect(screen.getByText("Output mode: transform")).toBeInTheDocument();
    expect(screen.getByText("Branches: branch-1, branch-2")).toBeInTheDocument();
    expect(screen.getByText("Policy: require all")).toBeInTheDocument();
    expect(screen.getByText("Merge: union")).toBeInTheDocument();
    expect(screen.getByText("Timeout: 7.25 seconds")).toBeInTheDocument();
    expect(screen.getByText("Schema mode: fixed")).toBeInTheDocument();
    expect(screen.getByText("id: int — required, non-null")).toBeInTheDocument();
    expect(screen.getByText("email: str — optional, nullable")).toBeInTheDocument();
    expect(screen.getByText("Write failure: quarantine")).toBeInTheDocument();
    expect(screen.queryByText(/\/private\//)).not.toBeInTheDocument();
  });

  it("renders row_union as N-to-N branch preservation, not coalesce or queue semantics", () => {
    const rowUnionId = "00000000-0000-4000-8000-000000000025";
    const nodes: WireStageData["nodes"] = [
      {
        stable_id: rowUnionId,
        label: "variant union",
        node_type: "row_union",
        plugin: null,
        behavior: {
          kind: "row_union",
          branch_aliases: ["branch-1", "branch-2"],
          policy: "require_all",
          timeout_seconds: 12.5,
        },
        node_options_summary: [],
        required_fields: [],
        guaranteed_fields: ["variant"],
        row_cardinality: {
          input: "branches",
          output: "one_per_branch",
          expected_output_count: null,
        },
        structured_output_fields: [],
      },
    ];
    const connections: WireStageData["connections"] = [
      {
        stable_id: "00000000-0000-4000-8000-000000000046",
        from_endpoint: { kind: "node", stable_id: rowUnionId },
        to_endpoint: { kind: "node", stable_id: NODE_ID },
        flow: { kind: "row_union_success", branch: null },
        schema_contract: null,
      },
    ];

    render(
      <WireStageTurn
        data={canonicalData({ nodes, connections })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );

    expect(
      screen.getByText("Cardinality: branches → one per branch"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Branches preserved: branch-1, branch-2"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Policy: wait for every branch, then forward each row"),
    ).toBeInTheDocument();
    expect(screen.getByText("Timeout: 12.5 seconds")).toBeInTheDocument();
    expect(screen.getByText("Row union success")).toBeInTheDocument();
    expect(screen.queryByText(/merge: union/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/queued items individually/i)).not.toBeInTheDocument();
  });

  it("renders detailed success, route, branch, and failure semantics with stable connection ids", () => {
    const GATE_ID = "00000000-0000-4000-8000-000000000024";
    const nodes: WireStageData["nodes"] = [
      ...canonicalData().nodes,
      {
        stable_id: GATE_ID,
        label: "triage",
        node_type: "gate",
        plugin: null,
        behavior: {
          kind: "gate",
          condition: "row['temperature'] > 40",
          route_aliases: ["route-1", "route-2"],
          routes: [
            { alias: "route-1", key: "hot" },
            { alias: "route-2", key: "cold" },
          ],
          fork_branches: [{ routes: ["route-2"], branch: "branch-1" }],
        },
        node_options_summary: [],
        required_fields: [],
        guaranteed_fields: [],
        row_cardinality: { input: "one", output: "one", expected_output_count: null },
        structured_output_fields: [],
      },
    ];
    const connections: WireStageData["connections"] = [
      ...canonicalData().connections,
      {
        stable_id: "00000000-0000-4000-8000-000000000042",
        from_endpoint: { kind: "node", stable_id: GATE_ID },
        to_endpoint: { kind: "output", stable_id: OUTPUT_ID },
        flow: { kind: "gate_route", route: "route-1", branch: null },
        schema_contract: null,
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000043",
        from_endpoint: { kind: "node", stable_id: GATE_ID },
        to_endpoint: { kind: "output", stable_id: OUTPUT_ID },
        flow: { kind: "gate_fork", routes: ["route-2"], branch: "branch-1" },
        schema_contract: null,
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000044",
        from_endpoint: { kind: "node", stable_id: NODE_ID },
        to_endpoint: { kind: "discard" },
        flow: { kind: "node_error" },
        schema_contract: null,
      },
      {
        stable_id: "00000000-0000-4000-8000-000000000045",
        from_endpoint: { kind: "output", stable_id: OUTPUT_ID },
        to_endpoint: { kind: "discard" },
        flow: { kind: "output_write_failure" },
        schema_contract: null,
      },
    ];

    render(<WireStageTurn data={canonicalData({ nodes, connections })} onConfirm={vi.fn()} confirmDisabled={false} />);

    // Flow semantics stay per-row; status moved out of the prose into chips
    // (operator-reported "— not yet checked" dump) with a single count line.
    expect(screen.getByText("Source success")).toBeInTheDocument();
    // F11: route rows resolve the ordinal to its author-visible key.
    expect(screen.getByText("Gate route route-1 (when hot)")).toBeInTheDocument();
    expect(screen.getByText("Gate fork route-2 (when cold) as branch-1")).toBeInTheDocument();
    // F11: the gate's own details name the condition and each route's target.
    expect(screen.getByText("When row['temperature'] > 40")).toBeInTheDocument();
    expect(screen.getByText("When hot → output-1 (route-1)")).toBeInTheDocument();
    expect(screen.getByText("When cold → output-1 (route-2)")).toBeInTheDocument();
    expect(screen.getByText("Node failure")).toBeInTheDocument();
    expect(screen.getByText("Output write failure")).toBeInTheDocument();
    expect(screen.getByText("6 routes — 1 connected, 5 not yet checked")).toBeInTheDocument();
    expect(screen.getAllByText("not yet checked")).toHaveLength(5);
    expect(screen.getByText(new RegExp(EDGE_ID))).toBeInTheDocument();
  });
});
