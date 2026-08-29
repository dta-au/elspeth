import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProgressView } from "./ProgressView";
import { useWebSocket } from "@/hooks/useWebSocket";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { makeComposition } from "@/test/composerFixtures";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";

vi.mock("@/hooks/useWebSocket", () => ({
  useWebSocket: vi.fn(),
}));

// Minimal RunProgress-shaped fixture; tests override only the fields they care
// about. The useWebSocket mock returns it untyped, mirroring the inline objects
// used by the other cases in this file.
function progressFixture(overrides: Record<string, unknown> = {}) {
  return {
    source_rows_processed: 0,
    tokens_succeeded: 0,
    tokens_failed: 0,
    tokens_quarantined: 0,
    tokens_routed_success: 0,
    tokens_routed_failure: 0,
    cancel_requested: false,
    accounting: null,
    recent_errors: [],
    status: "running",
    ...overrides,
  };
}

describe("ProgressView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(usePreferencesStore);
    resetStore(useSessionStore);
  });

  it("renders live progress with explicit source and token units", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: {
        source_rows_processed: 1,
        tokens_succeeded: 9_323,
        tokens_failed: 2,
        tokens_quarantined: 1,
        tokens_routed_success: 7,
        tokens_routed_failure: 2,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    render(<ProgressView />);

    expect(screen.getByText("Source rows")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Tokens succeeded")).toBeInTheDocument();
    expect(screen.getByText("9,323")).toBeInTheDocument();
    expect(screen.getByText("Tokens failed")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("7 routed success")).toBeInTheDocument();
    expect(screen.getByText("2 routed failure")).toBeInTheDocument();
    expect(screen.getByText("1 quarantined")).toBeInTheDocument();
  });

  it("shows a cancelling state after cancel is requested", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: {
        source_rows_processed: 1,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        cancel_requested: true,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    render(<ProgressView />);

    // The header is a ui/StatusBadge (elspeth-e1c5ad0b53); cancelling maps to
    // the cancelled colour family.
    const badge = screen.getByText("cancelling");
    expect(badge).toHaveClass("status-badge", "status-badge-cancelled");
    expect(screen.queryByRole("button", { name: "Cancel pipeline execution" })).not.toBeInTheDocument();
  });

  // elspeth-e1c5ad0b53: the status header adopts ui/StatusBadge so the
  // completed_with_failures / empty distinction carries the ⚠ / ∅ a11y glyphs
  // instead of being colour-only (the hand-rolled label dropped them).
  it("renders the completed_with_failures header as a StatusBadge with the ⚠ glyph", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "completed_with_failures" }),
    });

    render(<ProgressView />);

    const badge = screen.getByText("completed with failures");
    // Not aliased to -completed (elspeth-cd885f4c4d): a partial failure keeps
    // its own warning-family class rather than the unqualified success tint.
    expect(badge).toHaveClass(
      "status-badge",
      "status-badge-completed_with_failures",
    );
    expect(badge).toHaveTextContent("⚠");
  });

  it("renders the empty header as a StatusBadge with the ∅ glyph", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "empty" }),
    });

    render(<ProgressView />);

    const badge = screen.getByText("empty");
    expect(badge).toHaveClass("status-badge", "status-badge-empty");
    expect(badge).toHaveTextContent("∅");
  });

  it("shows closed accounting totals for structural-token DAG completions", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: {
        source_rows_processed: 1,
        tokens_succeeded: 9_323,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        cancel_requested: false,
        accounting: {
          source: { rows_processed: 1, rows_rejected: 0, rows_read: 1 },
          tokens: {
            emitted: 9_324,
            terminal: 9_324,
            succeeded: 9_323,
            failed: 0,
            structural: 1,
            pending: 0,
            abandoned: 0,
          },
          routing: {
            routed_success: 0,
            routed_failure: 0,
            quarantined: 0,
            discarded: 0,
          },
          integrity: {
            closure: "closed",
            missing_terminal_outcomes: 0,
            duplicate_terminal_outcomes: 0,
          },
        },
        recent_errors: [],
        status: "completed",
      },
    });

    render(<ProgressView />);

    expect(screen.getByLabelText("Run accounting")).toBeInTheDocument();
    expect(screen.getByText("Tokens emitted")).toBeInTheDocument();
    expect(screen.getAllByText("9,324")).toHaveLength(2);
    expect(screen.getByText("Tokens terminal")).toBeInTheDocument();
    expect(screen.getByText("Tokens structural")).toBeInTheDocument();
    expect(
      screen.getByText("Audit closure: complete — every row is accounted for."),
    ).toBeInTheDocument();
  });

  // M07 (WCAG 4.1.3): a single polite live region announces the run phase for
  // every terminal status, not just cancelled.
  it("announces the running phase through a polite live region", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "running" }),
    });

    render(<ProgressView />);

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Pipeline running.");
  });

  // A pending (queued) run must announce DIFFERENTLY from running, otherwise the
  // pending→running transition produces no DOM text change and the polite live
  // region never tells a screen-reader user the run actually started.
  it("announces a pending run distinctly from a running run", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "pending" }),
    });

    render(<ProgressView />);

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Pipeline queued.");
    expect(region).not.toHaveTextContent("Pipeline running.");
  });

  it("announces a completed terminal transition with totals via the live region", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({
        status: "completed",
        source_rows_processed: 3,
        tokens_succeeded: 2,
        tokens_failed: 1,
      }),
    });

    render(<ProgressView />);

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent(/Pipeline completed.*3 rows, 2 succeeded, 1 failed\./);
  });

  // The cases above each mount with their terminal status ALREADY set, so they
  // pin the announcement TEXT without ever performing the running -> terminal
  // transition their titles name. That leaves the mechanism unguarded: this is
  // the declared single terminal-announcement authority (M07), and a live
  // region only announces reliably when it pre-exists its content and the text
  // MUTATES inside it. Drive the real transition and hold the element across
  // it, so a refactor that remounts the region — restoring the unreliable
  // insert-with-content pattern — fails here rather than staying green.
  it("MUTATES the same live region across the running -> completed transition", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "running" }),
    });

    const { rerender } = render(<ProgressView />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent(/in progress|running/i);

    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({
        status: "completed",
        source_rows_processed: 3,
        tokens_succeeded: 3,
        tokens_failed: 0,
      }),
    });
    rerender(<ProgressView />);

    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent(/Pipeline completed/);
  });

  it("distinguishes completed-with-failures in the live announcement", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({
        status: "completed_with_failures",
        source_rows_processed: 4,
        tokens_succeeded: 3,
        tokens_failed: 1,
      }),
    });

    render(<ProgressView />);

    expect(screen.getByRole("status")).toHaveTextContent(
      /Pipeline completed with failures.*4 rows, 3 succeeded, 1 failed\./,
    );
  });

  it("announces a failed terminal transition even when recent errors are present", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({
        status: "failed",
        source_rows_processed: 5,
        tokens_succeeded: 1,
        tokens_failed: 4,
        recent_errors: [{ node_id: "rate_colours", message: "HTTP 400", row_id: null }],
      }),
    });

    render(<ProgressView />);

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent(/Pipeline failed.*5 rows, 1 succeeded, 4 failed\./);
  });

  it("announces cancellation through the live region (visible message is visual-only)", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "cancelled" }),
    });

    render(<ProgressView />);

    // The live region is the single announcement source; the visible
    // ``progress-cancelled-msg`` no longer carries a competing role.
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Pipeline execution was cancelled.");
    expect(screen.getAllByText("Pipeline execution was cancelled.")).toHaveLength(2);
  });

  // R2-F5 (elspeth-139a345050): the striped bar carried role="progressbar"
  // with a hardcoded "in progress" label in EVERY state, including terminal
  // ones — so a finished run still visually and semantically claimed to be
  // in progress. The colored strip itself stays (intentional per the Phase
  // 2.2 colour mapping above), but a terminal run must not expose
  // role="progressbar" or the "in progress" label.
  it("does not expose role=progressbar once the run reaches a terminal state", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "completed" }),
    });

    render(<ProgressView />);

    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Pipeline execution in progress")).not.toBeInTheDocument();
  });

  it("keeps role=progressbar with the in-progress label while the run is running", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "running" }),
    });

    render(<ProgressView />);

    expect(
      screen.getByRole("progressbar", { name: "Pipeline execution in progress" }),
    ).toBeInTheDocument();
  });

  it("labels the progressbar as queued before execution starts", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "pending" }),
    });

    render(<ProgressView />);

    expect(
      screen.getByRole("progressbar", { name: "Pipeline execution queued" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Pipeline execution in progress")).not.toBeInTheDocument();
  });

  // Mid-run counters need an "in progress" affordance so the numbers don't
  // read as a settled final tally while the run is still going.
  it("shows a running affordance near the counters while the run is in progress", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "running" }),
    });

    render(<ProgressView />);

    expect(screen.getByText("Running — counts so far")).toBeInTheDocument();
  });

  it("labels pending counters as queued rather than running", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "pending" }),
    });

    render(<ProgressView />);

    expect(screen.getByText("Queued — waiting to start")).toBeInTheDocument();
    expect(screen.queryByText("Running — counts so far")).not.toBeInTheDocument();
  });

  it("describes cancellation of a pending run as queued", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "pending" }),
    });

    render(<ProgressView />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel pipeline execution" }));

    expect(
      screen.getByText("Cancel the queued pipeline? This cannot be undone."),
    ).toBeInTheDocument();
  });

  it("hides the running affordance once the run is terminal", () => {
    (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
      activeRunId: "run-1",
      wsDisconnected: false,
      progress: progressFixture({ status: "completed" }),
    });

    render(<ProgressView />);

    expect(screen.queryByText("Running — counts so far")).not.toBeInTheDocument();
  });
});

function mountProgress(overrides: Record<string, unknown>) {
  (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
    activeRunId: "run-1",
    wsDisconnected: false,
    progress: progressFixture(overrides),
  });
  return render(<ProgressView />);
}

function accounting(integrity: Partial<{ closure: string; missing_terminal_outcomes: number; duplicate_terminal_outcomes: number }>) {
  return {
    source: { rows_processed: 1, rows_rejected: 0, rows_read: 1 },
    tokens: { emitted: 4, terminal: 4, succeeded: 4, failed: 0, structural: 0, pending: 0, abandoned: 0 },
    routing: { routed_success: 0, routed_failure: 0, quarantined: 0, discarded: 0 },
    integrity: { closure: "closed", missing_terminal_outcomes: 0, duplicate_terminal_outcomes: 0, ...integrity },
  };
}

describe("detail level (elspeth-05a240b82a)", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    useSessionStore.setState({ compositionState: null } as never);
  });

  it("keeps the closure verdict visible and collapses the six-cell grid by default", () => {
    const { container } = mountProgress({ status: "completed", accounting: accounting({}) });
    expect(screen.getByText("Audit closure: complete — every row is accounted for.")).toBeInTheDocument();
    const detail = screen.getByText("Accounting detail").closest("details") as HTMLElement;
    expect(detail).not.toBeNull();
    expect(detail).not.toHaveAttribute("open");
    expect(within(detail).getByText("Tokens emitted")).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("opens the grid when show_advanced is on, and keeps integrity warnings out of the disclosure", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    mountProgress({ status: "completed", accounting: accounting({ closure: "open", missing_terminal_outcomes: 2 }) });
    expect(screen.getByText("Accounting detail").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Missing terminal").closest("details")).toBeNull();
  });

  it("glosses quarantined rows", () => {
    mountProgress({ status: "completed", tokens_quarantined: 3 });
    expect(screen.getByText("3 quarantined", { selector: "span" })).toHaveAttribute(
      "title",
      "Quarantined rows are kept in the audit trail but excluded from the output.",
    );
  });

  it("summarises the recent-errors feed to a count and resolves node ids in the disclosure", () => {
    useSessionStore.setState({ compositionState: makeComposition(2) } as never);
    mountProgress({
      status: "failed",
      tokens_failed: 2,
      recent_errors: [
        { node_id: "select_columns", message: "boom", row_id: "r-1" },
        { node_id: "select_columns", message: "boom again", row_id: "r-2" },
      ],
    });
    const details = screen.getByText("2 rows failed — view details").closest("details") as HTMLElement;
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(details).queryAllByText(/^select_columns$/)).toHaveLength(0);
  });
});
