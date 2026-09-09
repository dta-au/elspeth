import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { stepLabelForPlugin } from "@/components/chat/interpretationStepLabel";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import { resetStore } from "@/test/store-helpers";
import type { NodeOptionSummary, WireStageData } from "@/types/guided";
import {
  buildEntityNames,
  reconstructWireEdges,
  routesSummaryText,
  routesWithoutStaticCheck,
  WireStageTurn,
  wireStagePlaceholder,
  type WireEdge,
} from "./WireStageTurn";
import type {
  WiringApprovalClientBlockers,
  WiringApprovalSignals,
} from "./wiringApproval";

// WireStageTurn reads show_advanced through useShowAdvanced(); the store is a
// module singleton, so every test in this file starts from the default (off).
beforeEach(() => resetStore(usePreferencesStore));

/** The routes-level raw-edge dump's expander. Every component row now carries
 *  its own "Technical details" summary too (elspeth-ca456d9d8d), so the pins
 *  on THIS one address it by its class. */
function rawDetails(container: HTMLElement): HTMLElement {
  const details = container.querySelector("details.wire-stage__raw");
  expect(details).not.toBeNull();
  return details as HTMLElement;
}

/** Text of a region with the per-row Technical details subtrees removed — a
 *  closed <details> still contributes to textContent, so "not in the default
 *  row" has to be asserted against the row MINUS its disclosure. */
function defaultRowText(region: HTMLElement): string {
  const clone = region.cloneNode(true) as HTMLElement;
  for (const details of clone.querySelectorAll("details.wire-stage__row-technical")) {
    details.remove();
  }
  return clone.textContent ?? "";
}

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

  it("names source and output edits as form-directed while retaining node and edge replanning", async () => {
    render(<WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} onCorrect={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Edit reviewed component" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit component settings" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Component"), OUTPUT_ID);
    expect(screen.getByRole("button", { name: "Edit component settings" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Component"), NODE_ID);
    expect(screen.getByRole("heading", { name: "Request a wiring correction" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-plan wiring" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Component"), EDGE_ID);
    expect(screen.getByRole("button", { name: "Re-plan wiring" })).toBeInTheDocument();
  });

  it("summarises route status once and renders per-route chips, not trailing prose", () => {
    // canonicalData: one satisfied contract (connected) + one null contract
    // on a route into an output (no static check). The per-line "— …"
    // dangling clause was the operator-reported debug-dump read; status
    // renders as a compact chip with the count summary above the list.
    render(<WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />);

    expect(screen.getByText("2 routes — 1 connected, 1 with no static check")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("no static check")).toBeInTheDocument();
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
        { name: "node-1 (Output) to output-1 — Node success — no static check" },
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
        "triage (Gate) to variant union (Row Union) — Gate fork route-1 (when control) as branch-1 — no static check",
        "triage (Gate) to variant union (Row Union) — Gate fork route-2 (when treatment) as branch-2 — no static check",
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
    const { container } = render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    expect(screen.getByText("Review expansion cardinality.")).toBeInTheDocument();
    expect(screen.getByText("Missing fields: body")).toBeInTheDocument();
    expect(within(rawDetails(container)).getByText("Technical details")).toBeInTheDocument();
    // The stable ids moved into the per-row disclosures (elspeth-ca456d9d8d).
    expect(within(container).getAllByText(/^Stable ID:/)).toHaveLength(3);
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

  // I-2 (design review 2026-09-02): the llm node's prompt and model render on
  // the wire card before Confirm wiring, ungated, and Edit pre-selects that
  // node in the existing correction form (the planner re-plans it).
  const LONG_PROMPT = Array.from(
    { length: 12 },
    (_, index) => `Step ${index + 1}: consider the passage carefully.`,
  ).join("\n");

  function llmData(): WireStageData {
    const base = canonicalData();
    return {
      ...base,
      nodes: [{
        ...base.nodes[0],
        plugin: "llm",
        node_options_summary: [
          { key: "model", value: "anthropic/claude-sonnet-4", tier: "common" },
          { key: "system_prompt", value: "You are a careful reviewer.", tier: "common" },
          { key: "prompt_template", value: LONG_PROMPT, tier: "common" },
        ],
      }],
    };
  }

  it("shows the llm node's model and prompts before Confirm wiring, outside the Technical details disclosure", async () => {
    render(<WireStageTurn data={llmData()} onConfirm={vi.fn()} confirmDisabled={false} />);

    expect(screen.getByText("Model: anthropic/claude-sonnet-4").closest("details")).toBeNull();
    expect(screen.getByText(/You are a careful reviewer\./).closest("details")).toBeNull();
    expect(screen.getByText(/Step 1: consider the passage carefully\./).closest("details")).toBeNull();
    expect(screen.queryByText(/Step 12: consider/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show full prompt for node-1" }));
    expect(screen.getByText(/Step 12: consider the passage carefully\./)).toBeInTheDocument();
    // No correction path offered → no Edit.
    expect(screen.queryByRole("button", { name: /Edit prompt/ })).toBeNull();
  });

  it("Edit on the prompt selects that node in the correction form and the request targets it", async () => {
    const onCorrect = vi.fn();
    render(<WireStageTurn data={llmData()} onConfirm={vi.fn()} confirmDisabled={false} onCorrect={onCorrect} />);

    // The form starts on the first target (the source); Edit moves it.
    expect(screen.getByLabelText("Component")).toHaveValue(SOURCE_ID);
    await userEvent.click(screen.getByRole("button", { name: "Edit prompt for node-1" }));
    expect(screen.getByLabelText("Component")).toHaveValue(NODE_ID);
    const feedback = screen.getByLabelText("What should change?");
    expect(feedback).toHaveFocus();
    await userEvent.type(feedback, "Ask for a two-sentence summary.");
    await userEvent.click(screen.getByRole("button", { name: "Re-plan wiring" }));
    expect(onCorrect).toHaveBeenCalledWith({ kind: "node", stable_id: NODE_ID }, "Ask for a two-sentence summary.");
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
    const { container } = render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    await userEvent.click(within(rawDetails(container)).getByText("Technical details"));

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
    // enforcement. The card must report only that no static verdict exists;
    // a row claiming "no required fields" would be flatly false.
    const base = canonicalData();
    const data = canonicalData({
      outputs: base.outputs.map((output) => ({ ...output, required_fields: ["mapped", "body"] })),
      connections: base.connections.map((connection) =>
        connection.to_endpoint.kind === "output"
          ? { ...connection, schema_contract: null }
          : connection),
    });
    const { container } = render(<WireStageTurn data={data} onConfirm={vi.fn()} confirmDisabled={false} />);
    await userEvent.click(within(rawDetails(container)).getByText("Technical details"));

    const rawRows = screen.getByText(/00000000-0000-4000-8000-000000000041/).textContent ?? "";
    expect(rawRows).toContain("(contract not statically checked)");
    expect(rawRows).not.toContain("no required fields");
    expect(rawRows).not.toContain("not applicable");
    // The sink genuinely requires those fields — the surface must still say so.
    expect(screen.getByText("Required fields: mapped, body")).toBeInTheDocument();
    // …and the plain-language sibling states the absence WITHOUT implying a
    // check still to come: this route is never statically verified, so
    // "not yet checked" was the one reading the payload cannot support.
    expect(screen.getAllByText(/no static check/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/not yet checked/)).not.toBeInTheDocument();
    // The run-time sentence is what replaces the implied promise — and it is
    // a claim about what THIS CARD carries, not about what the validator did.
    // A gate-routed edge can reach the card with a null contract after the
    // validator checked it and found it satisfied (the wire payload keys the
    // contract lookup on the graph edge while EdgeContract.from_id is the
    // walked-back producer), so "not verified before the run" would be false
    // on exactly that route. See elspeth-0edd5b73ec.
    expect(
      screen.getByText(
        "This card carries no static verdict for these routes; whatever their destination requires is enforced row by row when the pipeline runs.",
      ),
    ).toBeInTheDocument();
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
    // (the operator-reported per-row status dump) with a single count line.
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
    // Six routes: 1 satisfied, 3 null into a real consumer, and 2 null into
    // `discard` (a node_error and an output_write_failure). The two discard
    // routes are counted and labelled as themselves — a route to discard has
    // no consumer to check a contract against, so calling it unchecked would
    // invent an absence.
    expect(
      screen.getByText("6 routes — 1 connected, 3 with no static check, 2 discard routes"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("no static check")).toHaveLength(3);
    expect(screen.getAllByText("discard route")).toHaveLength(2);
    expect(screen.getByText(new RegExp(EDGE_ID))).toBeInTheDocument();
  });
});

describe("detail level (elspeth-ca456d9d8d)", () => {
  const withNodeOptions = (node_options_summary: NodeOptionSummary[]): WireStageData => {
    const base = canonicalData();
    return { ...base, nodes: [{ ...base.nodes[0], node_options_summary }] };
  };

  it("keeps cardinality enums, field lists, and raw plugin ids out of the default row; shows display names", () => {
    const { container } = render(
      <WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />,
    );
    const components = screen.getByRole("region", { name: "Reviewed components" });
    // Positive: the display name renders (field_mapper → "Output", inline_blob/json likewise).
    expect(within(components).getByText(`(${stepLabelForPlugin("field_mapper")})`)).toBeInTheDocument();
    expect(within(components).getByText(`(${stepLabelForPlugin("inline_blob")})`)).toBeInTheDocument();
    // Negatives, on a fixture that DOES carry these values by construction.
    expect(defaultRowText(components)).not.toMatch(/\(field_mapper\)|\(inline_blob\)|\(json\)/);
    expect(defaultRowText(components)).not.toMatch(/zero or many|Cardinality:|Required fields:|Guaranteed fields:/);
    expect(screen.getByText(/Validation failure:/)).toBeInTheDocument();
    // Scoped to the per-row class: the routes-level raw-edge dump
    // (`.wire-stage__raw`) also has a "Technical details" summary, is not
    // flag-controlled, and is untouched.
    const rowDetails = container.querySelectorAll("details.wire-stage__row-technical");
    expect(rowDetails).toHaveLength(3); // exactly one per source / node / output row of canonicalData()
    for (const details of rowDetails) {
      expect(details).not.toHaveAttribute("open");
    }
    expectNoIdentifiersInDefaultDom(container, {
      allowSelectors: [".wire-stage__row-technical", ".wire-stage__raw"],
    });
  });

  it("opens every per-row Technical details when show_advanced flips on a mounted turn", () => {
    const { container } = render(
      <WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />,
    );
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    const rowDetails = container.querySelectorAll("details.wire-stage__row-technical");
    expect(rowDetails).toHaveLength(3);
    for (const details of rowDetails) {
      expect(details).toHaveAttribute("open");
    }
    // The routes-level raw dump is deliberately NOT flag-controlled.
    expect(container.querySelector("details.wire-stage__raw")).not.toHaveAttribute("open");
  });

  it("shows common option pairs inline and advanced pairs only in the disclosure", () => {
    render(
      <WireStageTurn
        data={withNodeOptions([
          { key: "mapping", value: "a → b", tier: "common" },
          { key: "select_only", value: "only the mapped fields are kept", tier: "advanced" },
        ])}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(screen.getByText("Mapping: a → b").closest("details")).toBeNull();
    expect(
      screen.getByText("Select only: only the mapped fields are kept").closest("details"),
    ).not.toBeNull();
  });

  it("treats a tier-less pair as common (pre-tier durable payloads)", () => {
    render(
      <WireStageTurn
        data={withNodeOptions([{ key: "mapping", value: "a → b" }])}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(screen.getByText("Mapping: a → b").closest("details")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Wire-stage calibration (elspeth-e4c2ebb697).
//
// The card used to read "not yet checked" on every absent contract and put
// "N not yet checked" beside a green Confirm: it looked greener than the
// validation it reflected, and the phrase promised a check that never comes.
// The calibration has four parts, each pinned below — the discard/absent
// split, the zero-eliding roll-up plus its one run-time sentence, an enabled
// Confirm that states what it is accepting, and the composer placeholder as a
// function of live state.
// ---------------------------------------------------------------------------

const RUN_TIME_NOTE =
  "This card carries no static verdict for these routes; whatever their " +
  "destination requires is enforced row by row when the pipeline runs.";

/** A satisfied contract on every canonical route — the "nothing to caveat" state. */
function checkedConnections(): WireStageData["connections"] {
  return canonicalData().connections.map((connection) => ({
    ...connection,
    schema_contract: {
      from: "producer",
      to: "consumer",
      producer_guarantees: ["body", "mapped"],
      consumer_requires: ["body", "mapped"],
      missing_fields: [],
      satisfied: true,
    },
  }));
}

/** One route, into `discard`: an absent contract whose reason the payload
 *  DOES carry (there is no consumer to check against). */
function discardOnlyConnections(): WireStageData["connections"] {
  return [{
    stable_id: EDGE_ID,
    from_endpoint: { kind: "node", stable_id: NODE_ID },
    to_endpoint: { kind: "discard" },
    flow: { kind: "node_error" },
    schema_contract: null,
  }];
}

/** A local WireEdge for the pure roll-up helpers (WireEdge is this file's own
 *  view model, not a wire shape — no decoder or fixture owes it anything). */
function viewEdge(overrides: Partial<WireEdge> = {}): WireEdge {
  return {
    stable_id: EDGE_ID,
    from: NODE_ID,
    to: OUTPUT_ID,
    label: "node_success",
    flow: { kind: "node_success", branch: null },
    satisfied: null,
    missing_fields: [],
    ...overrides,
  };
}

/** The elements Confirm's accessible description actually points at, in
 *  order. Every id MUST resolve: a dangling aria-describedby is an axe
 *  violation (aria-valid-attr-value) and this component is audited. */
function confirmDescriptionClasses(container: HTMLElement): string[] {
  const confirm = screen.getByRole("button", { name: "Confirm wiring" });
  const described = confirm.getAttribute("aria-describedby");
  if (described === null) return [];
  return described.split(" ").filter((id) => id.length > 0).map((id) => {
    const element = container.ownerDocument.getElementById(id);
    expect(element, `aria-describedby id ${id} resolves to an element`).not.toBeNull();
    return (element as HTMLElement).className;
  });
}

describe("route status calibration (elspeth-e4c2ebb697)", () => {
  it("reads a route into discard as a discard route, with no absent-check wording", () => {
    render(
      <WireStageTurn
        data={canonicalData({ connections: discardOnlyConnections() })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );

    expect(screen.getByText("1 route — 1 discard route")).toBeInTheDocument();
    expect(screen.getByText("discard route")).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: "node-1 (Output) to Discard — Node failure — discard route" }),
    ).toBeInTheDocument();
    // Nothing about this route is unverified, so neither the note nor the
    // Confirm caption may appear — a caveat with no subject is noise.
    expect(screen.queryByText(/no static check/)).not.toBeInTheDocument();
    expect(screen.queryByText(RUN_TIME_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Confirming accepts/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not yet checked/)).not.toBeInTheDocument();
  });

  it("counts every category and elides the ones the pipeline does not have", () => {
    expect(routesSummaryText([
      viewEdge({ stable_id: "e1", satisfied: true }),
      viewEdge({ stable_id: "e2", satisfied: true }),
    ])).toBe("2 routes — 2 connected");
    expect(routesSummaryText([viewEdge({ stable_id: "e1" })])).toBe(
      "1 route — 1 with no static check",
    );
    expect(routesSummaryText([viewEdge({ stable_id: "e1", to: "discard" })])).toBe(
      "1 route — 1 discard route",
    );
    expect(routesSummaryText([
      viewEdge({ stable_id: "e1", satisfied: true }),
      viewEdge({ stable_id: "e2", satisfied: false }),
      viewEdge({ stable_id: "e3" }),
      viewEdge({ stable_id: "e4", to: "discard" }),
      viewEdge({ stable_id: "e5", to: "discard" }),
    ])).toBe(
      "5 routes — 1 connected, 1 not connected correctly, 1 with no static check, 2 discard routes",
    );
    // No routes at all: the heading alone, never a dangling em dash.
    expect(routesSummaryText([])).toBe("0 routes");
  });

  it("excludes discard destinations from the routes-without-a-check count", () => {
    expect(routesWithoutStaticCheck([
      viewEdge({ stable_id: "e1" }),
      viewEdge({ stable_id: "e2", to: "discard" }),
      viewEdge({ stable_id: "e3", satisfied: true }),
      viewEdge({ stable_id: "e4", satisfied: false }),
    ])).toBe(1);
    expect(routesWithoutStaticCheck([viewEdge({ stable_id: "e1", to: "discard" })])).toBe(0);
  });

  it("states the run-time consequence exactly once, only when a route lacks a check", () => {
    const { rerender } = render(
      <WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />,
    );
    expect(screen.getAllByText(RUN_TIME_NOTE)).toHaveLength(1);

    rerender(
      <WireStageTurn
        data={canonicalData({ connections: checkedConnections() })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(screen.queryByText(RUN_TIME_NOTE)).not.toBeInTheDocument();
  });
});

describe("Confirm wiring reflects what it accepts (elspeth-e4c2ebb697)", () => {
  it("stays ENABLED on routes with no static check and captions what confirming accepts", async () => {
    // ADR-007 abstention is admissible: `can_confirm` is `validation.is_valid`
    // and an absent contract does not make the composition invalid. So the
    // button must not be disabled — it must SAY what it is accepting.
    const onConfirm = vi.fn();
    render(<WireStageTurn data={canonicalData()} onConfirm={onConfirm} confirmDisabled={false} />);

    const confirm = screen.getByRole("button", { name: "Confirm wiring" });
    expect(confirm).toBeEnabled();
    expect(
      screen.getByText("Confirming accepts 1 route with no verdict on this card."),
    ).toBeInTheDocument();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("counts the caption's routes and drops it when every route carries a verdict", () => {
    const twoUnchecked = canonicalData({
      connections: canonicalData().connections.map((connection) => ({
        ...connection,
        schema_contract: null,
      })),
    });
    const { rerender } = render(
      <WireStageTurn data={twoUnchecked} onConfirm={vi.fn()} confirmDisabled={false} />,
    );
    expect(
      screen.getByText("Confirming accepts 2 routes with no verdict on this card."),
    ).toBeInTheDocument();

    rerender(
      <WireStageTurn
        data={canonicalData({ connections: checkedConnections() })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(screen.queryByText(/^Confirming accepts/)).not.toBeInTheDocument();
  });

  it("drops the caption when nothing can be confirmed, keeping the routes note", () => {
    // The caption is a claim about what pressing Confirm accepts, so it is
    // only honest while Confirm can be pressed. With a server blocker it used
    // to sit directly above "The pipeline isn't ready to confirm:", describing
    // what a dead button would accept. The routes NOTE is not gated with it —
    // that one is a fact about the routes, true either way.
    const { rerender } = render(
      <WireStageTurn
        data={canonicalData({ blockers: [{ message: "invalid route" }], can_confirm: false })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeDisabled();
    expect(screen.queryByText(/^Confirming accepts/)).not.toBeInTheDocument();
    expect(screen.getAllByText(RUN_TIME_NOTE)).toHaveLength(1);

    // A client-known validation issue disables Confirm for the same reason and
    // must silence the caption for the same reason.
    rerender(
      <WireStageTurn
        data={canonicalData()}
        onConfirm={vi.fn()}
        confirmDisabled={false}
        validationIssues={["Sink 'out' is missing a required field."]}
      />,
    );
    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeDisabled();
    expect(screen.queryByText(/^Confirming accepts/)).not.toBeInTheDocument();

    // An in-flight dispatch is NOT a statement about the pipeline: the caption
    // must not blink out mid-submit as though the caveat had been resolved.
    // This is also what keeps the acknowledgement gate below attributable —
    // it kills the lazier `!confirmBlocked && !confirmDisabled`, which would
    // silence the caption on every submit as well.
    rerender(
      <WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={true} />,
    );
    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeDisabled();
    expect(
      screen.getByText("Confirming accepts 1 route with no verdict on this card."),
    ).toBeInTheDocument();
  });

  it("drops the caption while an acknowledgement holds Confirm shut", () => {
    // The product-real shape: ChatPanel derives the Confirm `disabled` prop
    // and `wirePendingAcknowledgements` from the SAME predicate over the same
    // store slice (useHasPendingGuidedInterpretations is exactly
    // usePendingAcknowledgements(...).length > 0), so a pending card ALWAYS
    // arrives here as confirmDisabled=true + a non-empty blocker list.
    //
    // That state is durable, not in-flight: it persists until the user
    // resolves the card. So the caption — a claim about what pressing Confirm
    // accepts — must not render above a panel that says the press is
    // unavailable. It previously did, putting "Confirming accepts 1 route…"
    // directly on top of "1 acknowledgement pending — resolve it to enable
    // Confirm wiring:" with the button dead (WCAG 3.3.2 / 1.3.1: an
    // accessible description promising an action the control cannot perform).
    render(
      <WireStageTurn
        data={canonicalData()}
        onConfirm={vi.fn()}
        confirmDisabled={true}
        pendingAcknowledgements={[{ id: "event-1", label: "Confirm the mapping" }]}
      />,
    );

    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeDisabled();
    expect(
      screen.getByText("1 acknowledgement pending — resolve it to enable Confirm wiring:"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Confirming accepts/)).not.toBeInTheDocument();
    // The routes note is NOT gated with the caption: it is a fact about the
    // routes, true whether or not anything can be confirmed today.
    expect(screen.getAllByText(RUN_TIME_NOTE)).toHaveLength(1);
  });

  it("describes Confirm with exactly the panels that render, in blockers → routes → caption order", () => {
    // Routes note + caption, no blockers.
    const { container, rerender } = render(
      <WireStageTurn data={canonicalData()} onConfirm={vi.fn()} confirmDisabled={false} />,
    );
    expect(confirmDescriptionClasses(container)).toEqual([
      "wire-stage__routes-note",
      "wire-stage__confirm-note",
    ]);

    // A pending acknowledgement plus the unchecked route: blockers panel and
    // routes note, NO caption — the acknowledgement is a durable state that
    // holds Confirm shut until the user resolves the card, so a caption
    // saying what confirming accepts would describe a dead button.
    //
    // `confirmDisabled={false}` alongside an acknowledgement is NOT a shape
    // the product can produce (ChatPanel gates that prop on the very same
    // predicate that populates `pendingAcknowledgements` — see
    // useHasPendingGuidedInterpretations vs usePendingAcknowledgements). It is
    // written that way ON PURPOSE, to discriminate the gate: this case is what
    // kills a mutation that drops the acknowledgement term from the caption's
    // condition, which a product-real `confirmDisabled={true}` fixture cannot.
    rerender(
      <WireStageTurn
        data={canonicalData()}
        onConfirm={vi.fn()}
        confirmDisabled={false}
        pendingAcknowledgements={[{ id: "event-1", label: "Confirm the mapping" }]}
      />,
    );
    expect(confirmDescriptionClasses(container)).toEqual([
      "wire-stage__blockers",
      "wire-stage__routes-note",
    ]);

    // Blockers AND an unchecked route: the routes note still describes the
    // button (it is a fact about the routes), but the caption does not — it
    // would be describing what a dead button accepts. This is the case that
    // discriminates the caption's gate; a checked-routes fixture cannot,
    // because there is nothing to caption either way.
    rerender(
      <WireStageTurn
        data={canonicalData({
          blockers: [{ message: "invalid route" }],
          can_confirm: false,
        })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(confirmDescriptionClasses(container)).toEqual([
      "wire-stage__blockers",
      "wire-stage__routes-note",
    ]);

    // Blockers only: every route checked, so there is nothing to caveat.
    rerender(
      <WireStageTurn
        data={canonicalData({
          connections: checkedConnections(),
          blockers: [{ message: "invalid route" }],
          can_confirm: false,
        })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(confirmDescriptionClasses(container)).toEqual(["wire-stage__blockers"]);

    // Neither: no description attribute at all (a dangling id fails axe).
    rerender(
      <WireStageTurn
        data={canonicalData({ connections: checkedConnections() })}
        onConfirm={vi.fn()}
        confirmDisabled={false}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Confirm wiring" }),
    ).not.toHaveAttribute("aria-describedby");
  });
});

describe("wireStagePlaceholder (elspeth-e4c2ebb697)", () => {
  /** The wire card's own verdict, as `WireStageData` supplies it. */
  function wiring(overrides: Partial<WiringApprovalSignals> = {}): WiringApprovalSignals {
    return { can_confirm: true, blockers: [], warnings: [], ...overrides };
  }

  it("names the pending acknowledgement cards while any remain", () => {
    expect(wireStagePlaceholder({ pendingAcknowledgements: 1, validationIssues: 0 }, wiring())).toBe(
      "Resolve the 1 pending acknowledgement card, then press Confirm wiring.",
    );
    expect(wireStagePlaceholder({ pendingAcknowledgements: 3, validationIssues: 0 }, wiring())).toBe(
      "Resolve the 3 pending acknowledgement cards, then press Confirm wiring.",
    );
  });

  it("points at the card's named issues when the persisted composition is invalid", () => {
    expect(wireStagePlaceholder({ pendingAcknowledgements: 0, validationIssues: 2 }, wiring())).toBe(
      "Fix the issues named on the card, then press Confirm wiring.",
    );
  });

  it("points at the card's named issues when the SERVER refuses the confirm", () => {
    // The defect this arm closes. `can_confirm` / `blockers` are the usual
    // reason Confirm is off at step 4 — the pre-commit guided composition is
    // empty-by-design, so `validationIssues` is normally 0 there — and a
    // placeholder blind to them fell through to the last arm and told the
    // learner to press a disabled button. In the tutorial, whose read-only box
    // is empty, that was the only instruction on screen.
    const clean = { pendingAcknowledgements: 0, validationIssues: 0 };
    expect(wireStagePlaceholder(clean, wiring({ can_confirm: false }))).toBe(
      "Fix the issues named on the card, then press Confirm wiring.",
    );
    expect(wireStagePlaceholder(clean, wiring({ blockers: [{ message: "invalid route" }] }))).toBe(
      "Fix the issues named on the card, then press Confirm wiring.",
    );
  });

  it("names the two real controls when nothing blocks the confirm", () => {
    expect(wireStagePlaceholder({ pendingAcknowledgements: 0, validationIssues: 0 }, wiring())).toBe(
      "Press Confirm wiring on the card, or use its form to change a component.",
    );
    // Warnings deliberately do NOT block: they leave Confirm enabled on the
    // card, so the caption must keep naming the controls (approvalStopReason
    // treats them differently, and says why).
    expect(
      wireStagePlaceholder(
        { pendingAcknowledgements: 0, validationIssues: 0 },
        wiring({ warnings: [{ message: "observed schema" }] }),
      ),
    ).toBe("Press Confirm wiring on the card, or use its form to change a component.");
  });

  it("falls back to the client-known arms when no wire card is on screen", () => {
    // A step-4 session whose next turn is not `confirm_wiring` has no verdict
    // to lead with; the caption must still work.
    expect(wireStagePlaceholder({ pendingAcknowledgements: 0, validationIssues: 0 }, null)).toBe(
      "Press Confirm wiring on the card, or use its form to change a component.",
    );
    expect(wireStagePlaceholder({ pendingAcknowledgements: 1, validationIssues: 0 }, null)).toBe(
      "Resolve the 1 pending acknowledgement card, then press Confirm wiring.",
    );
  });

  it("leads with the server's verdict, because that message stays true", () => {
    // NOT the blockers panel's order. The card names the acknowledgement
    // cards too (as jump links), so "the issues named on the card" covers
    // every combination; an acknowledgement-first order would instead promise
    // that clearing the cards makes Confirm live while server blockers remain.
    expect(
      wireStagePlaceholder({ pendingAcknowledgements: 2, validationIssues: 0 }, wiring({ can_confirm: false })),
    ).toBe("Fix the issues named on the card, then press Confirm wiring.");
    // With the server content, the acknowledgement cards ARE the nearest
    // action and keep their more specific wording.
    expect(wireStagePlaceholder({ pendingAcknowledgements: 2, validationIssues: 2 }, wiring())).toBe(
      "Resolve the 2 pending acknowledgement cards, then press Confirm wiring.",
    );
  });

  it("names only controls that are on screen in the default render", () => {
    // The design specified "…or Re-plan wiring to change a component." and was
    // wrong about the code: `correctionTargets` is ordered sources → nodes →
    // connections → outputs and the select initialises on the first entry, so
    // the form opens on a SOURCE and submits as "Edit component settings"
    // under the heading "Edit reviewed component". Any pipeline that reached
    // step 4 has a source, so that is the unconditional default, not an edge
    // case — and reordering the targets to make the copy true would silently
    // change which component a blind Enter corrects.
    render(
      <WireStageTurn
        data={canonicalData()}
        onConfirm={vi.fn()}
        confirmDisabled={false}
        onCorrect={vi.fn()}
      />,
    );
    const arm = wireStagePlaceholder(
      { pendingAcknowledgements: 0, validationIssues: 0 },
      { can_confirm: true, blockers: [], warnings: [] },
    );

    expect(arm).toContain("Confirm wiring");
    expect(screen.getByRole("button", { name: "Confirm wiring" })).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Edit component settings" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-plan wiring" })).not.toBeInTheDocument();
    expect(arm).not.toContain("Re-plan wiring");
  });

  it("keeps every arm inside the measured ≤80-char placeholder budget", () => {
    // Past the 3-row box at the 360px pane a placeholder is clipped silently
    // (the budget GUIDED_CHAT_PLACEHOLDERS records); 12 is a plausible upper
    // bound on pending cards, and every arm — including the server-verdict one
    // — must be reachable in this table.
    const cases: Array<[WiringApprovalClientBlockers, WiringApprovalSignals | null]> = [
      [{ pendingAcknowledgements: 0, validationIssues: 0 }, wiring()],
      [{ pendingAcknowledgements: 0, validationIssues: 1 }, wiring()],
      [{ pendingAcknowledgements: 1, validationIssues: 0 }, wiring()],
      [{ pendingAcknowledgements: 12, validationIssues: 0 }, wiring()],
      [{ pendingAcknowledgements: 0, validationIssues: 0 }, wiring({ can_confirm: false })],
      [{ pendingAcknowledgements: 0, validationIssues: 0 }, null],
    ];
    for (const [blockers, signals] of cases) {
      expect(
        wireStagePlaceholder(blockers, signals).length,
        JSON.stringify([blockers, signals]),
      ).toBeLessThanOrEqual(80);
    }
  });
});
