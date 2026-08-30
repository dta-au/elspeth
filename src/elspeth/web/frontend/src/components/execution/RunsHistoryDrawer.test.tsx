import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunsHistoryDrawer } from "./RunsHistoryDrawer";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import { makeComposition } from "@/test/composerFixtures";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import type { RunDiagnostics } from "@/types/index";

const TEXTRACT_S3_UNREADABLE_HINT =
  "Amazon Textract could not read the S3 object. Most commonly the object is outside the " +
  "S3 read scope granted to the pipeline's AWS role (check the role's s3:GetObject prefix " +
  "against the document's bucket/key and, when version_field is configured, its " +
  "s3:GetObjectVersion permission); it can also mean the object does not exist or is stored " +
  "in a different region than the Textract endpoint. Verify access scope before suspecting " +
  "a corrupt or unsupported file.";

vi.mock("@/components/inspector/RunOutputsPanel", () => ({
  RunOutputsPanel: ({ runId }: { runId: string }) => (
    <div data-testid="run-outputs-panel" data-run-id={runId} />
  ),
}));

function makeDiagnostics(overrides: Partial<RunDiagnostics> = {}): RunDiagnostics {
  return {
    run_id: "r2",
    landscape_run_id: "r2",
    run_status: "failed",
    cancel_requested: false,
    summary: {
      token_count: 1,
      preview_limit: 50,
      preview_truncated: false,
      discard_count: 0,
      state_counts: { failed: 1 },
      operation_counts: { runtime_preflight: 1 },
      latest_activity_at: "2026-05-17T00:00:00Z",
    },
    tokens: [
      {
        token_id: "token-1",
        row_id: "row-1",
        row_index: 0,
        lineage: [],
        join_group_id: null,
        step_in_pipeline: null,
        created_at: "2026-05-17T00:00:00Z",
        terminal_outcome: "failed",
        states: [
          {
            state_id: "state-1",
            token_id: "token-1",
            node_id: "rate_colours",
            step_index: 0,
            attempt: 0,
            status: "failed",
            duration_ms: 12,
            started_at: "2026-05-17T00:00:00Z",
            completed_at: "2026-05-17T00:00:01Z",
            error: null,
            success_reason: null,
          },
        ],
      },
    ],
    operations: [
      {
        operation_id: "op-1",
        node_id: "rate_colours",
        operation_type: "runtime_preflight",
        // Only sink_write operations may carry a sink_effect_id
        // (ck_operations_sink_effect_type); preflight ops are null.
        sink_effect_id: null,
        status: "failed",
        duration_ms: 12,
        started_at: "2026-05-17T00:00:00Z",
        completed_at: "2026-05-17T00:00:01Z",
        error_message: "HTTP 400",
      },
    ],
    artifacts: [],
    discards: [],
    failure_detail: {
      operation_id: "op-1",
      node_id: "rate_colours",
      operation_type: "runtime_preflight",
      error_message: "HTTP 400: max_output_tokens below minimum value",
      failed_at: "2026-05-17T00:00:01Z",
    },
    ...overrides,
  };
}

// File-level, not per-describe: the drawer is a sessionStore reader on two
// counts — the empty-state session title and the curated failure row's node
// naming — so compositionState must not leak between tests in either block.
beforeEach(() => resetStore(useSessionStore));

describe("RunsHistoryDrawer", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    useExecutionStore.setState({
      runs: [
        { id: "r1", status: "completed" } as never,
        { id: "r2", status: "failed" } as never,
      ],
      activeRunId: null,
      progress: null,
      diagnosticsByRunId: {},
      diagnosticsLoadingByRunId: {},
      diagnosticsEvaluatingByRunId: {},
      diagnosticsErrorByRunId: {},
      diagnosticsExplanationByRunId: {},
      diagnosticsWorkingViewByRunId: {},
    } as never);
    useSessionStore.setState({
      activeSessionId: null,
      sessions: [],
    } as never);
  });

  it("lists every run from the store", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    expect(screen.getByText(/r1/)).toBeInTheDocument();
    expect(screen.getByText(/r2/)).toBeInTheDocument();
  });

  it("marks a run whose accounting failed audit validation, without hiding its siblings", () => {
    // elspeth-d5578ccd98: the backend ships accounting_corruption INSTEAD of
    // accounting for a corrupt run; the list must render the run with an
    // explicit marker while healthy runs stay ordinary.
    usePreferencesStore.setState({ showAdvanced: true });
    useExecutionStore.setState({
      runs: [
        { id: "r1", status: "completed" } as never,
        {
          id: "r2",
          status: "completed",
          accounting: null,
          accounting_corruption: {
            landscape_run_id: "land-r2",
            violations: ["2 token(s) with duplicate completed terminal outcomes"],
          },
        } as never,
      ],
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    const marker = screen.getByText("⚠ audit accounting corrupt");
    expect(marker).toBeInTheDocument();
    expect(marker).toHaveAttribute(
      "title",
      "2 token(s) with duplicate completed terminal outcomes",
    );
    expect(screen.getAllByText("⚠ audit accounting corrupt")).toHaveLength(1);
    expect(screen.getByText(/r1/)).toBeInTheDocument();
  });

  it("labels runs by stable ordinal and local start time, newest first", () => {
    const firstId = "c5f713ed-3bef-40d1-adda-7669d573efad";
    const secondId = "ec7f8f38-df73-4f55-ba8a-75c5c138733e";
    const firstStartedAt = "2026-07-12T01:15:00Z";
    const secondStartedAt = "2026-07-12T02:45:00Z";
    useExecutionStore.setState({
      runs: [
        {
          id: firstId,
          status: "completed",
          started_at: firstStartedAt,
        } as never,
        {
          id: secondId,
          status: "completed",
          started_at: secondStartedAt,
        } as never,
      ],
    } as never);
    usePreferencesStore.setState({ showAdvanced: true });

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    const formatter = new Intl.DateTimeFormat(undefined, {
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
    expect(
      screen.getByRole("heading", { name: "Run history · 2" }),
    ).toBeVisible();
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(
      `Run 2 · ${formatter.format(new Date(secondStartedAt))}`,
    );
    expect(items[0]).toHaveTextContent(secondId);
    expect(items[1]).toHaveTextContent(
      `Run 1 · ${formatter.format(new Date(firstStartedAt))}`,
    );
    expect(items[1]).toHaveTextContent(firstId);
  });

  // elspeth-e1c5ad0b53: run status renders through ui/StatusBadge so the
  // completed_with_failures / empty distinction carries the ⚠ / ∅ glyphs
  // rather than colour alone, and underscores read as spaces.
  it("renders run status as a StatusBadge with the a11y glyph map", () => {
    useExecutionStore.setState({
      runs: [
        { id: "r1", status: "completed_with_failures" } as never,
        { id: "r2", status: "empty" } as never,
      ],
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    const withFailures = screen.getByText("completed with failures");
    // Not aliased to -completed (elspeth-cd885f4c4d): a partial failure keeps
    // its own warning-family class rather than the unqualified success tint.
    expect(withFailures).toHaveClass(
      "status-badge",
      "status-badge-completed_with_failures",
    );
    expect(withFailures).toHaveTextContent("⚠");

    const empty = screen.getByText("empty");
    expect(empty).toHaveClass("status-badge", "status-badge-empty");
    expect(empty).toHaveTextContent("∅");
  });

  it("calls onClose when the Close button is clicked", async () => {
    const onClose = vi.fn();
    render(<RunsHistoryDrawer onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: /close runs/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when Escape is pressed", async () => {
    const onClose = vi.fn();
    render(<RunsHistoryDrawer onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("renders 'No prior runs' when the runs list is empty", () => {
    useExecutionStore.setState({ runs: [] } as never);
    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    expect(screen.getByText(/no prior runs/i)).toBeInTheDocument();
  });

  // elspeth-ef8c18a6cb (line-item): the empty state must follow the
  // title-first convention (HeaderSessionSwitcher), never the raw UUID.
  it("names the session by title, not UUID, in the empty state", () => {
    const sessionId = "3f2c9a10-0000-0000-0000-00000000abcd";
    useExecutionStore.setState({ runs: [] } as never);
    useSessionStore.setState({
      activeSessionId: sessionId,
      sessions: [
        {
          id: sessionId,
          title: "Colour survey",
          created_at: "",
          updated_at: "",
        },
      ],
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    expect(screen.getByText(/no prior runs for "Colour survey"/i)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(sessionId))).not.toBeInTheDocument();
  });

  it("falls back to 'this session' in the empty state when no title is known", () => {
    useExecutionStore.setState({ runs: [] } as never);
    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    expect(screen.getByText(/no prior runs for this session/i)).toBeInTheDocument();
  });

  // ── REST-backed cancel (elspeth-90db33baac) ────────────────────────────────
  //
  // ProgressView's Cancel needs the in-memory activeRunId + WebSocket; after
  // a reload those are gone. The drawer offers cancel on live rows via the
  // REST endpoint, gated by the same ConfirmDialog pattern.

  it("offers Cancel only on non-terminal runs", () => {
    useExecutionStore.setState({
      runs: [
        { id: "r1", status: "completed" } as never,
        { id: "r2", status: "running" } as never,
        { id: "r3", status: "pending" } as never,
      ],
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    expect(
      screen.queryByRole("button", { name: /^Cancel Run 1 · /i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Cancel Run 2 · /i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Cancel Run 3 · /i }),
    ).toBeInTheDocument();
  });

  it("cancels a running run through confirm", async () => {
    const cancel = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      runs: [{ id: "r2", status: "running" } as never],
      cancel,
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Cancel Run 1 · /i }));

    // Confirm gates the REST call.
    expect(cancel).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: /^cancel pipeline$/i }),
    );
    expect(cancel).toHaveBeenCalledWith("r2");
  });

  it("does not cancel when the confirm dialog is dismissed", async () => {
    const cancel = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      runs: [{ id: "r2", status: "running" } as never],
      cancel,
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Cancel Run 1 · /i }));
    await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(cancel).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("disables the row Cancel while cancellation is draining", () => {
    useExecutionStore.setState({
      runs: [
        { id: "r2", status: "running", cancel_requested: true } as never,
      ],
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);

    const button = screen.getByRole("button", { name: /^Cancel Run 1 · /i });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent(/cancelling/i);
  });

  it("loads and renders diagnostics detail for a selected run", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const loadRunDiagnostics = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      loadRunDiagnostics,
      diagnosticsByRunId: { r2: makeDiagnostics() },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    expect(loadRunDiagnostics).toHaveBeenCalledWith("r2");
    expect(screen.getByTestId("run-failure-detail")).toHaveTextContent(
      "max_output_tokens below minimum value",
    );
    expect(screen.getByText("token-1")).toBeInTheDocument();
    expect(screen.getByTestId("run-outputs-panel")).toHaveAttribute("data-run-id", "r2");
  });

  it("surfaces structured transform failure provenance from the node state", async () => {
    const diagnostics = makeDiagnostics({
      failure_detail: null,
      run_status: "completed_with_failures",
    });
    diagnostics.tokens[0].terminal_outcome = "routed_failure";
    diagnostics.tokens[0].states[0].node_id = "transform_textract_93c6c46b8b72";
    diagnostics.tokens[0].states[0].error = {
      reason: "submit_failed",
      error_type: "service_error",
      code: "InvalidS3ObjectException",
      cause: "s3_object_unreadable",
      error: TEXTRACT_S3_UNREADABLE_HINT,
    };
    useExecutionStore.setState({
      runs: [{ id: "r2", status: "completed_with_failures" } as never],
      diagnosticsByRunId: { r2: diagnostics },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent("failed - InvalidS3ObjectException");
    expect(failure.querySelector("[title]")).toHaveAttribute(
      "title",
      "transform_textract_93c6c46b8b72",
    );
    expect(failure).toHaveTextContent("Reason: the request could not be submitted");
    expect(failure).toHaveTextContent("Cause: the S3 object could not be read");
    // Known enums read as prose with the raw value recoverable from `title`.
    expect(
      within(failure).getByText("the request could not be submitted"),
    ).toHaveAttribute("title", "submit_failed");
    expect(failure).toHaveTextContent(TEXTRACT_S3_UNREADABLE_HINT);
    expect(failure).not.toHaveTextContent("service_error");
  });

  it("puts the raw diagnostic enum beside its phrase with Advanced on (elspeth-f49e1611ab)", async () => {
    // A run-failure reason or cause is exactly what a user pastes into a
    // support search. Before this it lived in `title` at both detail levels,
    // so a keyboard-only or touch user had no route to it. The phrase stays
    // and the raw enum joins it in <code>, the same register an UNPHRASED
    // value already uses — never dressed up as prose.
    const diagnostics = makeDiagnostics({
      failure_detail: null,
      run_status: "completed_with_failures",
    });
    diagnostics.tokens[0].terminal_outcome = "routed_failure";
    diagnostics.tokens[0].states[0].node_id = "transform_textract_93c6c46b8b72";
    diagnostics.tokens[0].states[0].error = {
      reason: "submit_failed",
      error_type: "service_error",
      code: "InvalidS3ObjectException",
      cause: "s3_object_unreadable",
      error: TEXTRACT_S3_UNREADABLE_HINT,
    };
    useExecutionStore.setState({
      runs: [{ id: "r2", status: "completed_with_failures" } as never],
      diagnosticsByRunId: { r2: diagnostics },
    } as never);
    usePreferencesStore.setState({ showAdvanced: true });

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent("Reason: the request could not be submitted");
    expect(within(failure).getByText("submit_failed").tagName).toBe("CODE");
    expect(within(failure).getByText("s3_object_unreadable").tagName).toBe("CODE");
    // The phrase and its `title` are unchanged — the raw value is an addition,
    // not a replacement.
    expect(
      within(failure).getByText("the request could not be submitted"),
    ).toHaveAttribute("title", "submit_failed");
  });

  it("rejects malformed diagnostic identifiers and falls back to the next valid field", async () => {
    const diagnostics = makeDiagnostics({ failure_detail: null });
    diagnostics.tokens[0].states[0].node_id = "content_safety";
    diagnostics.tokens[0].states[0].error = {
      code: "Invalid S3 Object<script>",
      error_type: "guardrail_service_error",
      reason: "api_error\nforged",
      cause: "provider_rejected",
    };
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: diagnostics },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent(
      "content_safety failed - guardrail_service_error",
    );
    expect(failure).toHaveTextContent("Cause: the provider rejected the request");
    expect(failure).not.toHaveTextContent("Invalid S3 Object");
    expect(failure).not.toHaveTextContent("forged");
  });

  it("rejects oversized identifiers before selecting a fallback label", async () => {
    const oversizedCode = "A".repeat(129);
    const oversizedErrorType = "b".repeat(129);
    const diagnostics = makeDiagnostics({ failure_detail: null });
    diagnostics.tokens[0].states[0].node_id = "transform_textract";
    diagnostics.tokens[0].states[0].error = {
      code: oversizedCode,
      error_type: oversizedErrorType,
      reason: "submit_failed",
    };
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: diagnostics },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent("failed - submit_failed");
    expect(failure.querySelector("[title]")).toHaveAttribute("title", "transform_textract");
    expect(failure).not.toHaveTextContent(oversizedCode);
    expect(failure).not.toHaveTextContent(oversizedErrorType);
  });

  it("rejects a forged S3-unreadable tuple with arbitrary free text", async () => {
    const forgedHint =
      "Amazon Textract could not read the S3 object. Most commonly the object is outside the arbitrary provider text.";
    const diagnostics = makeDiagnostics({ failure_detail: null });
    diagnostics.tokens[0].states[0].node_id = "transform_textract";
    diagnostics.tokens[0].states[0].error = {
      reason: "submit_failed",
      error_type: "service_error",
      code: "InvalidS3ObjectException",
      cause: "s3_object_unreadable",
      error: forgedHint,
    };
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: diagnostics },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent("failed - InvalidS3ObjectException");
    expect(failure.querySelector("[title]")).toHaveAttribute("title", "transform_textract");
    expect(failure).not.toHaveTextContent("arbitrary provider text");
  });

  it("names the failed node and falls back to error_type without a provider code", async () => {
    const diagnostics = makeDiagnostics({ failure_detail: null });
    diagnostics.tokens[0].states[0].node_id = "content_safety";
    diagnostics.tokens[0].states[0].error = {
      reason: "api_error",
      error_type: "guardrail_service_error",
      error: "unclassified provider response text must stay audit-only",
    };
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: diagnostics },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    const failure = screen.getByTestId("run-state-failure-state-1");
    expect(failure).toHaveTextContent(
      "content_safety failed - guardrail_service_error",
    );
    expect(failure).toHaveTextContent("Reason: api_error");
    expect(failure).not.toHaveTextContent("unclassified provider response text");
  });

  it("shows the stored run failure cause immediately before diagnostics load", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const loadRunDiagnostics = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      runs: [
        {
          id: "r2",
          status: "failed",
          error: "HTTP 400: max_output_tokens below minimum value",
        } as never,
      ],
      loadRunDiagnostics,
      diagnosticsByRunId: {},
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1 · /i }));

    expect(screen.getByTestId("run-stored-failure-detail")).toHaveTextContent(
      "max_output_tokens below minimum value",
    );
  });

  it("keeps the stored run failure cause visible when diagnostics have no failure_detail", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    useExecutionStore.setState({
      runs: [
        {
          id: "r2",
          status: "failed",
          error: "Pipeline aborted before runtime diagnostics were written.",
        } as never,
      ],
      diagnosticsByRunId: { r2: makeDiagnostics({ failure_detail: null }) },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1 · /i }));

    expect(screen.queryByTestId("run-failure-detail")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-stored-failure-detail")).toHaveTextContent(
      "Pipeline aborted before runtime diagnostics were written.",
    );
  });

  it("renders the diagnostics working view while explanation is pending", async () => {
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: makeDiagnostics() },
      diagnosticsEvaluatingByRunId: { r2: true },
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));

    expect(screen.getByText("Reading current run evidence")).toBeInTheDocument();
    expect(screen.getByText("1 token is visible in the runtime trace.")).toBeInTheDocument();
  });

  it("requests an LLM diagnostics explanation for a selected run", async () => {
    const evaluateRunDiagnostics = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      diagnosticsByRunId: { r2: makeDiagnostics() },
      evaluateRunDiagnostics,
    } as never);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 2 · /i }));
    await userEvent.click(screen.getByRole("button", { name: /explain/i }));

    expect(evaluateRunDiagnostics).toHaveBeenCalledWith("r2");
  });

  it("moves focus into the drawer on open (Close button receives focus)", () => {
    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: /close/i })).toHaveFocus();
  });

  it("traps Tab and Shift+Tab inside the drawer", async () => {
    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    const closeBtn = screen.getByRole("button", { name: /close/i });
    const firstDetail = screen.getByRole("button", { name: /^Show detail for Run 1 · /i });
    closeBtn.focus();
    await userEvent.tab();
    expect(firstDetail).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(closeBtn).toHaveFocus();
  });

  // M08 (WCAG 2.4.3): the drawer is aria-modal with no backdrop/inerting, so
  // focus can land on a control behind it (a click or a global shortcut). Tab
  // must then pull focus back into the drawer rather than walk the page behind.
  it("recaptures focus that has escaped the drawer on Tab", () => {
    const outside = document.createElement("button");
    outside.textContent = "underlying control";
    document.body.appendChild(outside);

    render(<RunsHistoryDrawer onClose={vi.fn()} />);
    outside.focus();
    expect(outside).toHaveFocus();

    fireEvent.keyDown(document, { key: "Tab" });

    expect(screen.getByRole("button", { name: /close/i })).toHaveFocus();

    outside.remove();
  });

  // M08 (WCAG 2.4.3): closing the drawer must return focus to the control that
  // opened it, so keyboard users are not dumped at the top of the document.
  it("restores focus to the opener when the drawer unmounts", () => {
    const opener = document.createElement("button");
    opener.textContent = "Open past runs";
    document.body.appendChild(opener);
    opener.focus();
    expect(opener).toHaveFocus();

    const { unmount } = render(<RunsHistoryDrawer onClose={vi.fn()} />);
    // Focus moved into the drawer (the Close button) while open.
    expect(screen.getByRole("button", { name: /close/i })).toHaveFocus();

    unmount();
    expect(opener).toHaveFocus();

    opener.remove();
  });
});

describe("detail level (elspeth-34e810312c)", () => {
  const UUID_RUN_ID = "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0";
  const uuidRun = (status: string, extra: Record<string, unknown> = {}) =>
    ({ id: UUID_RUN_ID, status, started_at: "2026-08-29T10:00:00Z", ...extra }) as never;

  beforeEach(() => resetStore(usePreferencesStore));

  it("keeps the UUID out of visible text and aria-labels with the flag off, but in the label title", () => {
    const { container } = render(
      <RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("running")]} />,
    );
    expectNoIdentifiersInDefaultDom(container);
    expect(screen.getByText(/^Run 1 · /)).toHaveAttribute("title", UUID_RUN_ID);
    expect(screen.getByRole("button", { name: /^Cancel Run 1 · / })).toBeInTheDocument();
  });

  it("shows the UUID span when show_advanced is on", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("completed")]} />);
    expect(screen.getByText(UUID_RUN_ID)).toBeInTheDocument();
  });

  it("gates the token/operation lists and raw failure <pre> behind the flag; keeps count, Explain, and the curated failure detail", async () => {
    // The curated row is the only diagnostics content left visible with the
    // flag off, so it is held to the whole default-DOM acceptance pin — not a
    // hand-rolled negative over two ids. Its node id and all three diagnostic
    // identifiers (code, reason, cause) are seeded so the pin actually
    // exercises every raw value the row can render.
    useSessionStore.setState({
      compositionState: makeComposition(2, {
        nodes: [
          {
            id: "rate_colours",
            node_type: "transform",
            plugin: "llm",
            input: "source",
            on_success: null,
            on_error: null,
            options: {},
          },
        ],
      }),
    } as never);
    useExecutionStore.setState({
      loadRunDiagnostics: vi.fn().mockResolvedValue(undefined),
      diagnosticsByRunId: {
        [UUID_RUN_ID]: makeDiagnostics({
          run_id: UUID_RUN_ID,
          tokens: [
            {
              ...makeDiagnostics().tokens[0],
              states: [
                {
                  ...makeDiagnostics().tokens[0].states[0],
                  error: {
                    code: "some_code",
                    reason: "submit_failed",
                    cause: "s3_object_unreadable",
                  },
                },
              ],
            },
          ],
        }),
      },
    } as never);
    render(<RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("failed")]} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1/ }));
    expect(screen.getByText(/1 token/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain" })).toBeInTheDocument();
    const drawer = screen.getByRole("dialog", { name: "Pipeline runs" });
    expect(drawer.querySelector(".run-diagnostics-tokens")).toBeNull();
    expect(drawer.querySelector(".run-diagnostics-operations")).toBeNull();
    expect(screen.queryByTestId("run-failure-detail")).not.toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(drawer);
    // Curated authored surface stays (closed identifiers + authored hint), with
    // the node NAMED from the composition — the same phrase map ProgressView
    // uses — and the raw id recoverable from the row's title.
    const stateFailure = screen.getByTestId("run-state-failure-state-1");
    expect(stateFailure).toHaveTextContent("Rate Colours failed - some_code");
    expect(stateFailure.querySelector("[title]")).toHaveAttribute("title", "rate_colours");
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(drawer.querySelector(".run-diagnostics-tokens")).not.toBeNull();
    expect(screen.getByTestId("run-failure-detail")).toBeInTheDocument();
    // One render site: still exactly one curated failure row with the flag on.
    expect(screen.getAllByTestId("run-state-failure-state-1")).toHaveLength(1);
  });

  it("keeps the accounting-corruption badge regardless of the flag", () => {
    render(
      <RunsHistoryDrawer
        onClose={vi.fn()}
        runsOverride={[uuidRun("completed", { accounting_corruption: { violations: ["duplicate terminal outcome"] } })]}
      />,
    );
    expect(screen.getByText("⚠ audit accounting corrupt")).toBeInTheDocument();
  });
});
