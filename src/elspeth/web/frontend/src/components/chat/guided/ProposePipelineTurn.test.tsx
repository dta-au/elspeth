import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  GuidedProposalReviewState,
  ProposePipelinePayload,
} from "@/types/guided";
import { ProposePipelineTurn } from "./ProposePipelineTurn";

const IDS = {
  proposal: "00000000-0000-4000-8000-000000000401",
  sourceOne: "00000000-0000-4000-8000-000000000402",
  sourceTwo: "00000000-0000-4000-8000-000000000403",
  gate: "00000000-0000-4000-8000-000000000404",
  queue: "00000000-0000-4000-8000-000000000405",
  aggregation: "00000000-0000-4000-8000-000000000406",
  coalesce: "00000000-0000-4000-8000-000000000407",
  outputOne: "00000000-0000-4000-8000-000000000408",
  outputTwo: "00000000-0000-4000-8000-000000000409",
} as const;

function edgeId(index: number): string {
  return `00000000-0000-4000-8000-${String(500 + index).padStart(12, "0")}`;
}

function payload(): ProposePipelinePayload {
  const edges: ProposePipelinePayload["graph"]["edges"] = [
    {
      stable_id: edgeId(1),
      from_endpoint: { kind: "source", stable_id: IDS.sourceOne },
      to_endpoint: { kind: "node", stable_id: IDS.gate },
      flow: { kind: "source_success", branch: null },
    },
    {
      stable_id: edgeId(2),
      from_endpoint: { kind: "source", stable_id: IDS.sourceOne },
      to_endpoint: { kind: "discard" },
      flow: { kind: "source_validation_failure" },
    },
    {
      stable_id: edgeId(3),
      from_endpoint: { kind: "source", stable_id: IDS.sourceTwo },
      to_endpoint: { kind: "node", stable_id: IDS.queue },
      flow: { kind: "source_success", branch: null },
    },
    {
      stable_id: edgeId(4),
      from_endpoint: { kind: "source", stable_id: IDS.sourceTwo },
      to_endpoint: { kind: "discard" },
      flow: { kind: "source_validation_failure" },
    },
    {
      stable_id: edgeId(5),
      from_endpoint: { kind: "node", stable_id: IDS.queue },
      to_endpoint: { kind: "node", stable_id: IDS.gate },
      flow: { kind: "queue_continue", branch: null },
    },
    {
      stable_id: edgeId(6),
      from_endpoint: { kind: "node", stable_id: IDS.gate },
      to_endpoint: { kind: "node", stable_id: IDS.aggregation },
      flow: { kind: "gate_fork", routes: ["route-1"], branch: "branch-1" },
    },
    {
      stable_id: edgeId(7),
      from_endpoint: { kind: "node", stable_id: IDS.gate },
      to_endpoint: { kind: "node", stable_id: IDS.coalesce },
      flow: { kind: "gate_fork", routes: ["route-1"], branch: "branch-2" },
    },
    {
      stable_id: edgeId(8),
      from_endpoint: { kind: "node", stable_id: IDS.gate },
      to_endpoint: { kind: "output", stable_id: IDS.outputTwo },
      flow: { kind: "gate_route", route: "route-2", branch: null },
    },
    {
      stable_id: edgeId(9),
      from_endpoint: { kind: "node", stable_id: IDS.aggregation },
      to_endpoint: { kind: "node", stable_id: IDS.coalesce },
      flow: { kind: "node_success", branch: "branch-1" },
    },
    {
      stable_id: edgeId(10),
      from_endpoint: { kind: "node", stable_id: IDS.aggregation },
      to_endpoint: { kind: "discard" },
      flow: { kind: "node_error" },
    },
    {
      stable_id: edgeId(11),
      from_endpoint: { kind: "node", stable_id: IDS.coalesce },
      to_endpoint: { kind: "output", stable_id: IDS.outputOne },
      flow: { kind: "coalesce_success", branch: null },
    },
    {
      stable_id: edgeId(12),
      from_endpoint: { kind: "output", stable_id: IDS.outputOne },
      to_endpoint: { kind: "discard" },
      flow: { kind: "output_write_failure" },
    },
    {
      stable_id: edgeId(13),
      from_endpoint: { kind: "output", stable_id: IDS.outputTwo },
      to_endpoint: { kind: "discard" },
      flow: { kind: "output_write_failure" },
    },
  ];
  return {
    proposal_id: IDS.proposal,
    draft_hash: "d".repeat(64),
    supersedes_draft_hash: null,
    summary: "guided.proposal.summary.full_graph.v1",
    rationale: "guided.proposal.rationale.review_required.v1",
    component_counts: { sources: 2, nodes: 4, edges: edges.length, outputs: 2 },
    blockers: [],
    graph: {
      sources: [
        {
          stable_id: IDS.sourceOne,
          label: "source-1",
          plugin: { kind: "source", id: "csv" },
        },
        {
          stable_id: IDS.sourceTwo,
          label: "source-2",
          plugin: { kind: "source", id: "json" },
        },
      ],
      edges,
    },
    nodes: [
      {
        stable_id: IDS.gate,
        label: "node-1",
        node_type: "gate",
        plugin: null,
        behavior: {
          kind: "gate",
          condition: "row['amount'] > 500",
          route_aliases: ["route-1", "route-2"],
          routes: [
            { alias: "route-1", key: "true" },
            { alias: "route-2", key: "false" },
          ],
          fork_branches: [
            { routes: ["route-1"], branch: "branch-1" },
            { routes: ["route-1"], branch: "branch-2" },
          ],
        },
        node_options_summary: [],
      },
      {
        stable_id: IDS.queue,
        label: "node-2",
        node_type: "queue",
        plugin: null,
        behavior: { kind: "queue" },
        node_options_summary: [],
      },
      {
        stable_id: IDS.aggregation,
        label: "node-3",
        node_type: "aggregation",
        plugin: { kind: "transform", id: "batch" },
        behavior: {
          kind: "aggregation",
          trigger_kinds: ["count", "timeout"],
          count: "50",
          timeout_seconds: 10,
          output_mode: "passthrough",
          expected_output_count: "2",
        },
        node_options_summary: [],
      },
      {
        stable_id: IDS.coalesce,
        label: "node-4",
        node_type: "coalesce",
        plugin: null,
        behavior: {
          kind: "coalesce",
          branch_aliases: ["branch-1", "branch-2"],
          policy: "quorum",
          merge: "nested",
          timeout_seconds: 12.5,
        },
        node_options_summary: [],
      },
    ],
    outputs: [
      {
        stable_id: IDS.outputOne,
        label: "output-1",
        plugin: { kind: "sink", id: "json" },
      },
      {
        stable_id: IDS.outputTwo,
        label: "output-2",
        plugin: { kind: "sink", id: "csv" },
      },
    ],
    edit_targets: [
      { kind: "source", stable_id: IDS.sourceOne },
      { kind: "node", stable_id: IDS.gate },
      { kind: "edge", stable_id: edgeId(6) },
      { kind: "output", stable_id: IDS.outputOne },
    ],
  };
}

function activeReview(): GuidedProposalReviewState {
  return {
    status: "active",
    proposal_id: IDS.proposal,
    draft_hash: "d".repeat(64),
  };
}

describe("ProposePipelineTurn", () => {
  it("renders the full DAG, stable edge identities, virtual discard, routes, queues, aggregations, fan-in, sources, and outputs", () => {
    const { container } = render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByRole("img", { name: /pipeline proposal graph/i })).toBeVisible();
    expect(container.querySelector(`[data-edge-id="${edgeId(6)}"]`)).not.toBeNull();
    expect(container.querySelector('[data-node-kind="discard"]')).not.toBeNull();
    expect(screen.getByText("2 sources · 4 nodes · 13 routes · 2 outputs")).toBeVisible();
    // F11: the gate summary carries the authored predicate verbatim plus each
    // author-visible route key resolved to its destination — with the ordinal
    // aliases still visible (they are the revise-target / integrity tokens).
    expect(
      screen.getByText(
        /When row\['amount'\] > 500 — true → node-3 \+ node-4 \(route-1\), false → output-2 \(route-2\)\. 2 fork branches\./,
      ),
    ).toBeVisible();
    expect(screen.getByText(/queue continues in sequence/i)).toBeVisible();
    expect(screen.getByText(/count 50 or timeout 10s/i)).toBeVisible();
    expect(
      screen.getByText(
        /joins branch-1, branch-2 using quorum \/ nested; timeout 12.5s/i,
      ),
    ).toBeVisible();
    // F11: edge labels resolve route ordinals to "when <key>" while keeping
    // the ordinal alias visible.
    expect(screen.getAllByText(/when true \(route-1\) forks to branch-1/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/when false \(route-2\)/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/on error → discard/i).length).toBeGreaterThan(0);
    expect(screen.getByText("source-2 · json")).toBeVisible();
    expect(screen.getByText("output-2 · csv")).toBeVisible();
  });

  it("renders the key transform options beside the behavior discriminant", () => {
    // R2-F3: every transform read as "Transforms each incoming item.", so a
    // field_mapper's renames and its drop-the-rest projection were invisible
    // on the card the operator accepts.
    const base = payload();
    const mapperPayload: ProposePipelinePayload = {
      ...base,
      component_counts: { sources: 0, nodes: 1, edges: 0, outputs: 0 },
      graph: { sources: [], edges: [] },
      nodes: [
        {
          stable_id: IDS.aggregation,
          label: "node-1",
          node_type: "transform",
          plugin: { kind: "transform", id: "field_mapper" },
          behavior: { kind: "transform" },
          node_options_summary: [
            { key: "mapping", value: "given_name → first_name, meta.source → origin" },
            { key: "select_only", value: "only the mapped fields are kept" },
          ],
        },
      ],
      outputs: [],
      edit_targets: [],
    };

    render(
      <ProposePipelineTurn
        payload={mapperPayload}
        reviewState={activeReview()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText(/Transforms each incoming item\./)).toBeVisible();
    expect(
      screen.getByText("Mapping: given_name → first_name, meta.source → origin"),
    ).toBeVisible();
    expect(screen.getByText("Select only: only the mapped fields are kept")).toBeVisible();
  });

  it("renders row_union as a distinct N-to-N barrier with its own success flow and honest copy", () => {
    const rowUnionPayload = payload();
    rowUnionPayload.nodes[3] = {
      ...rowUnionPayload.nodes[3],
      node_type: "row_union",
      behavior: {
        kind: "row_union",
        branch_aliases: ["branch-1", "branch-2"],
        policy: "require_all",
        timeout_seconds: 12.5,
      },
      node_options_summary: [],
    };
    rowUnionPayload.graph.edges[10] = {
      ...rowUnionPayload.graph.edges[10],
      flow: { kind: "row_union_success", branch: null },
    };

    const { container } = render(
      <ProposePipelineTurn
        payload={rowUnionPayload}
        reviewState={activeReview()}
        onSubmit={vi.fn()}
      />,
    );

    expect(
      container.querySelector('[data-node-kind="row_union"]'),
    ).not.toBeNull();
    expect(
      container.querySelector(
        ".guided-readonly-graph__node--row_union",
      ),
    ).not.toBeNull();
    expect(
      screen.getByText(
        /waits for branch-1, branch-2, then forwards every row without merging records; timeout 12.5s/i,
      ),
    ).toBeVisible();
    expect(
      screen.getAllByText(/after row union → output-1/i).length,
    ).toBeGreaterThan(0);
  });

  it("distinguishes parallel row-union revision controls by gate-fork flow", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const rowUnionPayload = payload();
    rowUnionPayload.nodes[3] = {
      ...rowUnionPayload.nodes[3],
      node_type: "row_union",
      behavior: {
        kind: "row_union",
        branch_aliases: ["branch-1", "branch-2"],
        policy: "require_all",
        timeout_seconds: null,
      },
      node_options_summary: [],
    };
    rowUnionPayload.graph.edges[5] = {
      ...rowUnionPayload.graph.edges[5],
      to_endpoint: { kind: "node", stable_id: IDS.coalesce },
    };
    rowUnionPayload.graph.edges[6] = {
      ...rowUnionPayload.graph.edges[6],
      to_endpoint: { kind: "node", stable_id: IDS.coalesce },
    };
    rowUnionPayload.edit_targets = [
      { kind: "edge", stable_id: edgeId(6) },
      { kind: "edge", stable_id: edgeId(7) },
    ];

    render(
      <ProposePipelineTurn
        payload={rowUnionPayload}
        reviewState={activeReview()}
        onSubmit={onSubmit}
      />,
    );

    const control = screen.getByRole("button", {
      name: "Revise route from node-1 to node-4: when true (route-1) forks to branch-1",
    });
    const treatment = screen.getByRole("button", {
      name: "Revise route from node-1 to node-4: when true (route-1) forks to branch-2",
    });
    expect(control).toHaveTextContent(
      "Revise route from node-1 to node-4: when true (route-1) forks to branch-1",
    );
    expect(treatment).toHaveTextContent(
      "Revise route from node-1 to node-4: when true (route-1) forks to branch-2",
    );

    await user.click(treatment);
    expect(onSubmit).not.toHaveBeenCalled();
    await user.type(
      screen.getByRole("textbox", { name: "What should change?" }),
      "Keep branch-1 unchanged and send only this fork to branch-2.",
    );
    await user.click(screen.getByRole("button", { name: "Send revision request" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      edit_target: { kind: "edge", stable_id: edgeId(7) },
      correction_feedback: "Keep branch-1 unchanged and send only this fork to branch-2.",
    }));
  });

  it("uses fixed local copy for server template ids and never renders template ids as rationale", () => {
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("A complete pipeline is ready for review.")).toBeVisible();
    expect(
      screen.getByText("Review its structure, routes, and blockers before checking the detailed wiring."),
    ).toBeVisible();
    expect(screen.queryByText("guided.proposal.rationale.review_required.v1")).toBeNull();
  });

  it("collects exact feedback before submitting a proposal-bound node revision", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={onSubmit}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Review wiring" }));
    await user.click(screen.getByRole("button", { name: "Reject proposal" }));
    await user.click(screen.getByRole("button", { name: "Revise node-1" }));

    expect(onSubmit).toHaveBeenCalledTimes(2);
    const feedback = screen.getByRole("textbox", { name: "What should change?" });
    const submitRevision = screen.getByRole("button", { name: "Send revision request" });
    expect(submitRevision).toBeDisabled();
    await user.type(feedback, "Keep the threshold, but route the true branch to output-1 only.");
    await user.click(submitRevision);

    expect(onSubmit.mock.calls).toEqual([
      [{
        chosen: ["review_wiring"],
        edited_values: null,
        custom_inputs: null,
        proposal_id: IDS.proposal,
        draft_hash: "d".repeat(64),
        edit_target: null,
        control_signal: null,
      }],
      [{
        chosen: null,
        edited_values: null,
        custom_inputs: null,
        proposal_id: IDS.proposal,
        draft_hash: "d".repeat(64),
        edit_target: null,
        control_signal: "reject",
      }],
      [{
        chosen: null,
        edited_values: null,
        custom_inputs: null,
        proposal_id: IDS.proposal,
        draft_hash: "d".repeat(64),
        edit_target: { kind: "node", stable_id: IDS.gate },
        correction_feedback: "Keep the threshold, but route the true branch to output-1 only.",
        control_signal: null,
      }],
    ]);
  });

  it("supports keyboard activation and moves focus to proposal status changes", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const { rerender } = render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={onSubmit}
      />,
    );
    const accept = screen.getByRole("button", { name: "Review wiring" });
    accept.focus();
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);

    rerender(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={{
          status: "stale",
          proposal_id: IDS.proposal,
          draft_hash: "d".repeat(64),
        }}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByRole("status")).toHaveFocus();
    expect(accept).toBeDisabled();
  });

  it("renders allowlisted blockers, disables wiring review, and keeps stable revise controls available", () => {
    const blocked = payload();
    blocked.blockers = [
      {
        code: "policy_review_required",
        category: "policy",
        summary: "guided.proposal.blocker.policy_review_required.v1",
        edit_target: { kind: "node", stable_id: IDS.gate },
      },
    ];
    render(
      <ProposePipelineTurn
        payload={blocked}
        reviewState={activeReview()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("A policy review is required before this pipeline can advance.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Review wiring" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Revise node-1" })).toBeEnabled();
  });

  it("leaves submitting activity to the parent Current Decision footer", () => {
    // ChatPanel owns one canonical pending location for every guided turn.
    // Rendering a second status inside the proposal body duplicates the same
    // live phase above the graph and again in the Current Decision footer.
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={{
          status: "submitting",
          proposal_id: IDS.proposal,
          draft_hash: "d".repeat(64),
        }}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("Submitting this proposal decision…")).toBeNull();
    expect(screen.queryByText(/00:00/)).toBeNull();
  });

  it.each(["submitting", "reloading", "stale"] as const)(
    "disables the exact proposal controls while review state is %s",
    (status) => {
      render(
        <ProposePipelineTurn
          payload={payload()}
          reviewState={{
            status,
            proposal_id: IDS.proposal,
            draft_hash: "d".repeat(64),
          }}
          onSubmit={vi.fn()}
        />,
      );
      expect(screen.getByRole("button", { name: "Review wiring" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Reject proposal" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Revise node-1" })).toBeDisabled();
    },
  );

  it.each([
    ["accept", { kind: "review_wiring" }, "Review wiring"],
    ["reject", { kind: "reject" }, "Reject proposal"],
    [
      "one exact revise target",
      {
        kind: "revise",
        edit_target: { kind: "node", stable_id: IDS.gate },
        correction_feedback: "Keep the threshold and change only the true destination.",
      },
      "Send revision request",
    ],
  ] as const)(
    "enables only the retained %s action after an ambiguous transport failure",
    (_label, retryAction, enabledName) => {
      render(
        <ProposePipelineTurn
          payload={payload()}
          reviewState={{
            status: "error",
            proposal_id: IDS.proposal,
            draft_hash: "d".repeat(64),
            message: "The response was not received. Retry the same action.",
            retryable: true,
            retry_action: retryAction,
          } as never}
          onSubmit={vi.fn()}
        />,
      );

      expect(screen.getByRole("alert")).toHaveTextContent(/response was not received/i);
      const controls = screen.getAllByRole("button");
      expect(controls.filter((control) => !control.hasAttribute("disabled"))).toHaveLength(1);
      expect(screen.getByRole("button", { name: enabledName })).toBeEnabled();
    },
  );

  it("retries only the exact retained prose instruction and destructive mode", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={{
          status: "error",
          proposal_id: IDS.proposal,
          draft_hash: "d".repeat(64),
          message: "The response was not received. Retry the same action.",
          retryable: true,
          retry_action: {
            kind: "revise_instruction",
            revision_instruction: "Replace the topology with one audited transform.",
            revision_mode: "replace",
          },
        }}
        onSubmit={onSubmit}
      />,
    );

    const retry = screen.getByRole("button", { name: "Retry replace revision" });
    expect(retry).toBeEnabled();
    expect(screen.getByRole("button", { name: "Review wiring" })).toBeDisabled();
    await user.click(retry);
    expect(onSubmit).toHaveBeenCalledWith({
      chosen: null,
      edited_values: {
        revision_instruction: "Replace the topology with one audited transform.",
        revision_mode: "replace",
      },
      custom_inputs: null,
      edit_target: null,
      control_signal: null,
      proposal_id: IDS.proposal,
      draft_hash: "d".repeat(64),
    });
  });

  it("locks the stale proposal controls when an authoritative reload fails", () => {
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={{
          status: "error",
          proposal_id: IDS.proposal,
          draft_hash: "d".repeat(64),
          message: "The authoritative replacement could not be loaded.",
          retryable: false,
          retry_action: null,
        }}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/could not be loaded/i);
    expect(screen.getByRole("button", { name: "Review wiring" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject proposal" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Revise node-1" })).toBeDisabled();
  });

  it("keeps the live Review wiring primary on a tutorial revision proposal while withholding reject/revise", async () => {
    // Post-7.1 the tutorial's transforms phase yields a REAL planner proposal
    // (no canned recipe exhibit exists), so the primary must render and
    // dispatch — read-only rendering made the tutorial unfinishable. The
    // off-script affordances (Reject discards the build; Revise re-enters
    // planner rounds) stay withheld for the passive learner, mirroring
    // InspectAndConfirmTurn's hidden "Edit columns…". The primary is live
    // only once the frozen-prompt revision has superseded the pre-Send
    // auto-proposal (supersedes_draft_hash is set).
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const revision = { ...payload(), supersedes_draft_hash: "e".repeat(64) };
    render(
      <ProposePipelineTurn
        payload={revision}
        reviewState={activeReview()}
        onSubmit={onSubmit}
        isTutorial
      />,
    );
    expect(screen.getByRole("img", { name: /pipeline proposal graph/i })).toBeVisible();
    expect(screen.getByText("source-1 · csv")).toBeVisible();
    expect(screen.getByText(/press Review wiring to continue/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Reject proposal" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Revise/ })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Review wiring" }));
    expect(onSubmit.mock.calls).toEqual([
      [{
        chosen: ["review_wiring"],
        edited_values: null,
        custom_inputs: null,
        proposal_id: IDS.proposal,
        draft_hash: "d".repeat(64),
        edit_target: null,
        control_signal: null,
      }],
    ]);
  });

  it("withholds Review wiring on the tutorial pre-Send auto-proposal and tells the learner to press Send", () => {
    // Tutorial run 18 (session 07e8a3a8): the step-2→step-3 transition
    // auto-plans a first proposal from the degenerate fallback intent before
    // the learner's frozen transforms prompt is sent — a source→sink
    // passthrough. Accepting it commits a transform-less pipeline that the
    // tutorial launch gate then 409s. The auto-proposal is identified by
    // supersedes_draft_hash === null; withhold the primary and direct the
    // learner to Send so the revision re-plan carries the real scenario.
    const onSubmit = vi.fn();
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={onSubmit}
        isTutorial
      />,
    );
    expect(screen.getByRole("img", { name: /pipeline proposal graph/i })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Review wiring" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject proposal" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Revise/ })).toBeNull();
    expect(screen.getByText(/press Send/i)).toBeVisible();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the non-tutorial surface unchanged for a first proposal with no superseded draft", () => {
    // Live guided mode legitimately accepts a root-intent auto-proposal
    // (the colour flow depends on it) — the null discriminator must gate
    // nothing outside tutorial mode.
    const onSubmit = vi.fn();
    render(
      <ProposePipelineTurn
        payload={payload()}
        reviewState={activeReview()}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByRole("button", { name: "Review wiring" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject proposal" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Revise node-1" })).toBeEnabled();
  });
});
