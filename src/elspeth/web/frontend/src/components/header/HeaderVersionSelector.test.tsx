import { readFileSync } from "node:fs";

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { HeaderVersionSelector } from "./HeaderVersionSelector";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionStateVersion, NodeSpec } from "@/types/index";

// Wire-shaped version rows: GET /state/versions delivers full
// CompositionStateResponse rows (nodes/edges/outputs/sources/metadata),
// NOT a node_count — the selector derives the count from nodes.length.
function makeNode(id: string, options: Record<string, unknown> = {}): NodeSpec {
  return {
    id,
    node_type: "transform",
    plugin: "field_mapper",
    input: "source",
    on_success: null,
    on_error: null,
    options,
  };
}

function makeVersion(
  overrides: Partial<CompositionStateVersion> & {
    id: string;
    version: number;
  },
): CompositionStateVersion {
  return {
    created_at: "2026-05-15T10:00:00Z",
    sources: { main: { plugin: "csv_source", options: { path: "in.csv" } } },
    nodes: [makeNode(`n${overrides.version}`)],
    edges: [],
    outputs: [],
    metadata: { name: "pipeline", description: null },
    ...overrides,
  };
}

// Distinct nodes per version so no row is snapshot-only unless a test
// deliberately makes adjacent content identical.
const defaultVersions = [
  makeVersion({ id: "st-1", version: 1, nodes: [makeNode("a")] }),
  makeVersion({ id: "st-2", version: 2, nodes: [makeNode("a"), makeNode("b")] }),
  makeVersion({
    id: "st-3",
    version: 3,
    nodes: [makeNode("a"), makeNode("b"), makeNode("c")],
  }),
];

describe("HeaderVersionSelector", () => {
  beforeEach(() => {
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionState: {
        version: 3,
        sources: {},
        nodes: [],
        edges: [],
        outputs: [],
      } as never,
      messages: [],
      stateVersions: [],
      isLoadingVersions: false,
      loadStateVersions: vi.fn(),
      revertToVersion: vi.fn(),
    } as never);
  });

  it("renders nothing when no active session", () => {
    useSessionStore.setState({ activeSessionId: null } as never);
    const { container } = render(<HeaderVersionSelector />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the current composition version label", () => {
    render(<HeaderVersionSelector />);
    expect(screen.getByText(/v3|version 3/i)).toBeInTheDocument();
  });

  it("uses the design-spec label 'Composition history' on the dropdown trigger", () => {
    render(<HeaderVersionSelector />);
    expect(
      screen.getByRole("button", { name: /composition history/i }),
    ).toBeInTheDocument();
  });

  it("calls loadStateVersions when the dropdown opens", () => {
    const loadStateVersions = vi.fn();
    useSessionStore.setState({ loadStateVersions } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    expect(loadStateVersions).toHaveBeenCalled();
  });

  it("confirms and calls revertToVersion when the user picks an older version", () => {
    const revertToVersion = vi.fn();
    useSessionStore.setState({
      stateVersions: defaultVersions,
      revertToVersion,
    } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );
    fireEvent.click(
      screen.getByRole("option", { name: /^version 2 — edited$/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: /revert to version 2/i }));
    fireEvent.click(screen.getByRole("button", { name: /^revert$/i }));

    expect(revertToVersion).toHaveBeenCalledWith("st-2");
  });

  // Regression for the undefined node_count bug: fetched rows carry nodes,
  // not node_count, and the count must come from nodes.length.
  it("renders the real node count for fetched rows", () => {
    useSessionStore.setState({ stateVersions: defaultVersions } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    const option = screen.getByRole("option", {
      name: /^version 2 — edited$/i,
    });
    expect(within(option).getByText("2 nodes")).toBeInTheDocument();
  });

  // The plural case above has an oracle; this is the singular one it was
  // missing. v1 of defaultVersions has exactly one node.
  it("renders a single-node version as '1 node', not '1 nodes'", () => {
    useSessionStore.setState({ stateVersions: defaultVersions } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    const option = screen.getByRole("option", {
      name: /^version 1 — session created$/i,
    });
    expect(within(option).getByText("1 node")).toBeInTheDocument();
    expect(within(option).queryByText("1 nodes")).not.toBeInTheDocument();
  });

  it("labels version 1 as the session seed", () => {
    useSessionStore.setState({ stateVersions: defaultVersions } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    expect(
      screen.getByRole("option", { name: /^version 1 — session created$/i }),
    ).toBeInTheDocument();
  });

  it("labels a version from its applied tool-call stamp and joins it into the option name", () => {
    useSessionStore.setState({
      stateVersions: defaultVersions,
      messages: [
        {
          id: "msg-1",
          session_id: "sess-1",
          role: "assistant",
          content: "",
          tool_calls: [
            {
              id: "call-1",
              type: "function",
              function: { name: "upsert_edge", arguments: "{}" },
              outcome: "applied",
              applied_state_version: 2,
            },
          ],
          created_at: "2026-05-15T10:10:00Z",
        },
      ],
    } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    const option = screen.getByRole("option", {
      name: /^version 2 — applied: upsert_edge$/i,
    });
    expect(within(option).getByText("Applied: upsert_edge")).toBeInTheDocument();
  });

  // Snapshot-only rows carry nothing a user can decide on ("no visible
  // change"), so they are hidden from the history list entirely. Version
  // numbers stay REAL — they must keep agreeing with the chat revert
  // message and the audit trail — so the list shows honest gaps.
  it("hides snapshot-only rows from the history list", () => {
    // v2 content-identical to v1 — a bookkeeping snapshot; v3 differs.
    const v1 = makeVersion({ id: "st-1", version: 1, nodes: [makeNode("a")] });
    const v2 = makeVersion({ id: "st-2", version: 2, nodes: [makeNode("a")] });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      nodes: [makeNode("a"), makeNode("b")],
    });
    useSessionStore.setState({ stateVersions: [v1, v2, v3] } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    expect(
      screen.queryByRole("option", { name: /^version 2/i }),
    ).not.toBeInTheDocument();
    // Classification runs against the FULL fetched list: v3 is judged
    // relative to its true predecessor v2 even though v2 is hidden.
    expect(
      screen.getByRole("option", { name: /^version 3 \(current\) — edited$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /^version 1 — session created$/i }),
    ).toBeInTheDocument();
  });

  it("keeps the current version visible and labeled when it is itself snapshot-only", () => {
    const v1 = makeVersion({ id: "st-1", version: 1, nodes: [makeNode("a")] });
    const v2 = makeVersion({
      id: "st-2",
      version: 2,
      nodes: [makeNode("a"), makeNode("b")],
    });
    // v3 (current) content-identical to v2: the current row is the anchor
    // the trigger displays, so it stays visible with its honest label.
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      nodes: [makeNode("a"), makeNode("b")],
    });
    useSessionStore.setState({ stateVersions: [v1, v2, v3] } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    const current = screen.getByRole("option", {
      name: /^version 3 \(current\) — no visible change$/i,
    });
    expect(current.className).toContain("version-selector-item--snapshot");
    expect(
      within(current).getByText("no visible change"),
    ).toBeInTheDocument();
    // The stronger claim is not made anywhere on the row: the predicate
    // compares a redacted projection and cannot prove the pipeline itself
    // was unchanged.
    expect(current.textContent).not.toContain("no pipeline change");
    expect(current.getAttribute("aria-label")).not.toContain(
      "no pipeline change",
    );
    // v2 differs from v1 and stays visible.
    expect(
      screen.getByRole("option", { name: /^version 2 — edited$/i }),
    ).toBeInTheDocument();
  });

  // Hiding happens only on POSITIVE evidence of "no visible change".
  // A row whose predecessor is outside the fetched pagination window
  // cannot be classified and must stay visible.
  it("keeps a row visible when its predecessor is outside the fetched window", () => {
    const v2 = makeVersion({ id: "st-2", version: 2, nodes: [makeNode("a")] });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      nodes: [makeNode("a"), makeNode("b")],
    });
    // v1 is not in the fetched list — the window edge.
    useSessionStore.setState({ stateVersions: [v2, v3] } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );

    expect(
      screen.getByRole("option", { name: /^version 2 — edited$/i }),
    ).toBeInTheDocument();
  });

  it("reverts to a visible version across a hidden snapshot-only gap", () => {
    const revertToVersion = vi.fn();
    const v1 = makeVersion({ id: "st-1", version: 1, nodes: [makeNode("a")] });
    // v2 hidden (identical to v1); v3 differs.
    const v2 = makeVersion({ id: "st-2", version: 2, nodes: [makeNode("a")] });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      nodes: [makeNode("a"), makeNode("b")],
    });
    useSessionStore.setState({
      stateVersions: [v1, v2, v3],
      revertToVersion,
    } as never);
    render(<HeaderVersionSelector />);

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );
    fireEvent.click(
      screen.getByRole("option", { name: /^version 1 — session created$/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /revert to version 1/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^revert$/i }));

    expect(revertToVersion).toHaveBeenCalledWith("st-1");
  });

  // elspeth-83eb51334f: focus leaving the selector subtree closes the
  // dropdown — a keyboard user must not be able to Tab away while the
  // listbox stays visually open.
  it("closes the dropdown when focus moves outside the selector", () => {
    render(
      <div>
        <HeaderVersionSelector />
        <button type="button">outside</button>
      </div>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /composition history/i }),
    );
    const list = screen.getByRole("listbox", { name: /composition history/i });
    const outside = screen.getByRole("button", { name: /^outside$/i });
    fireEvent.blur(list, { relatedTarget: outside });

    expect(
      screen.queryByRole("listbox", { name: /composition history/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps the dropdown open when focus moves within the selector", () => {
    render(<HeaderVersionSelector />);

    const trigger = screen.getByRole("button", {
      name: /composition history/i,
    });
    fireEvent.click(trigger);
    const list = screen.getByRole("listbox", { name: /composition history/i });
    fireEvent.blur(list, { relatedTarget: trigger });

    expect(
      screen.getByRole("listbox", { name: /composition history/i }),
    ).toBeInTheDocument();
  });
});

// ── Header control sizing (elspeth-2d29ccf56e) ──────────────────────────────
//
// The trigger composed `.btn`, which binds to the 44px --size-control rung,
// inside the 40px --size-header-height band: 2px of the button was clipped at
// the top of the header and the overflow bled into the workspace below, at
// every viewport taller than 800px. (header.css compacts this exact selector
// under `@media (min-width: 961px) and (max-height: 800px)` — precisely the
// regime that did NOT clip.)
//
// This is deliberately an arithmetic test against the tokens, not a
// class-name spelling test: it asks whether the rung the trigger binds to
// actually fits the band it lives in.
describe("header version selector control rung", () => {
  const tokensCss = readFileSync("src/styles/tokens.css", "utf8");

  function tokenPx(name: string): number {
    const declared = new RegExp(`${name}\\s*:\\s*(\\d+)px`).exec(tokensCss);
    if (declared === null) {
      throw new Error(`${name} is not declared as a px value in tokens.css`);
    }
    return Number(declared[1]);
  }

  /** min-height each button base class binds to, from tokens.css. */
  const rungs: Record<string, number> = {
    btn: tokenPx("--size-control"),
    "btn-compact": tokenPx("--size-control-compact"),
  };

  it("binds the trigger to a rung that fits inside the header band", () => {
    render(<HeaderVersionSelector />);
    const trigger = screen.getByRole("button", {
      name: /composition history/i,
    });

    const composed = Object.keys(rungs).filter((base) =>
      trigger.classList.contains(base),
    );
    expect(
      composed,
      "the trigger must compose exactly one declared button rung — " +
        "tokens.css:248 forbids redeclaring min-height literally",
    ).toHaveLength(1);

    // .app-header is `height: var(--size-header-height)` with a 1px bottom
    // border inside that box (box-sizing: border-box is global), so the
    // content box is one pixel shorter than the band.
    const contentBox = tokenPx("--size-header-height") - 1;
    expect(rungs[composed[0]]).toBeLessThanOrEqual(contentBox);
  });
});
