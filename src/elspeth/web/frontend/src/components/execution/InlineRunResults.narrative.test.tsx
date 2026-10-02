import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { NarrativeResults } from "@/components/composer/NarrativeResults";
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { Run, RunOutputArtifact, RunOutputArtifactPreview } from "@/types/index";
import { InlineRunResults } from "./InlineRunResults";

vi.mock("@/hooks/useNarrativeMode", () => ({
  useNarrativeMode: () => ({ narrativeMode: true, isLoading: false }),
}));
vi.mock("@/api/client", async () => ({
  ...await vi.importActual<typeof import("@/api/client")>("@/api/client"),
  fetchRunOutputs: vi.fn(),
  fetchRunOutputPreview: vi.fn(),
  downloadRunOutputContent: vi.fn(),
}));

function run(id: string): Run {
  return {
    id, session_id: "session-a", status: "completed", accounting: null, error: null,
    started_at: "2026-05-19T10:00:00Z", finished_at: "2026-05-19T10:05:00Z", composition_version: 1,
  };
}

function artifact(runId: string): RunOutputArtifact {
  return {
    artifact_id: `artifact-${runId}`, sink_node_id: "results", producer_kind: "sink_effect",
    produced_by_state_id: null, sink_effect_id: `effect-${runId}`, artifact_type: "file",
    path_or_uri: `${runId}.jsonl`, content_hash: "a".repeat(64), size_bytes: 20,
    publication_performed: true, publication_evidence_kind: "returned",
    created_at: "2026-05-19T10:05:00Z", exists_now: true, downloadable: true, storage_kind: "sink_file",
  };
}

function preview(runId: string): RunOutputArtifactPreview {
  return {
    artifact_id: `artifact-${runId}`, content_type: "jsonl",
    preview_text: JSON.stringify({ summary: `Summary for ${runId}` }),
    truncated: false, total_size_bytes: 20, row_count_preview: 1,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

describe("narrative results run ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(useInterpretationEventsStore);
    useSessionStore.setState({ activeSessionId: "session-a", compositionState: null });
    useExecutionStore.setState({
      activeRunId: null, progress: null, runs: [run("run-a"), run("run-b")],
      loadRuns: vi.fn().mockResolvedValue("loaded"),
    });
    vi.mocked(api.fetchRunOutputs).mockImplementation(async (runId) => ({
      run_id: runId, landscape_run_id: runId, artifacts: [artifact(runId)],
    }));
    vi.mocked(api.fetchRunOutputPreview).mockImplementation(async (runId) => preview(runId));
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads and downloads the completed history run without a live attachment", async () => {
    vi.mocked(api.downloadRunOutputContent).mockResolvedValue({ data: new Blob(["output"]), filename: "run-a.jsonl" });
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:run-a");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<InlineRunResults showEmptyState />);

    await waitFor(() => expect(screen.getByText("Summary for run-a")).toBeInTheDocument());
    expect(useExecutionStore.getState().activeRunId).toBeNull();
    expect(api.fetchRunOutputs).toHaveBeenCalledWith("run-a");
    fireEvent.click(screen.getByTestId("narrative-results-download-link"));
    await waitFor(() => expect(api.downloadRunOutputContent).toHaveBeenCalledWith("run-a", "artifact-run-a"));
  });

  it("hides the prior summary and download while the selected run loads", async () => {
    const nextPreview = deferred<RunOutputArtifactPreview>();
    const { rerender } = render(<NarrativeResults runId="run-a" />);
    await waitFor(() => expect(screen.getByText("Summary for run-a")).toBeInTheDocument());
    vi.mocked(api.fetchRunOutputPreview).mockImplementation(() => nextPreview.promise);

    rerender(<NarrativeResults runId="run-b" />);
    expect(screen.queryByText("Summary for run-a")).not.toBeInTheDocument();
    expect(screen.queryByTestId("narrative-results-download-link")).not.toBeInTheDocument();
    await act(async () => { nextPreview.resolve(preview("run-b")); });
    expect(screen.getByText("Summary for run-b")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("narrative-results-download-link"));
    expect(api.downloadRunOutputContent).toHaveBeenCalledWith("run-b", "artifact-run-b");
  });

  it("does not let a late prior-run preview overwrite the selected run", async () => {
    const oldPreview = deferred<RunOutputArtifactPreview>();
    vi.mocked(api.fetchRunOutputPreview).mockImplementation((runId) =>
      runId === "run-a" ? oldPreview.promise : Promise.resolve(preview(runId)),
    );
    const { rerender } = render(<NarrativeResults runId="run-a" />);
    await waitFor(() => expect(api.fetchRunOutputPreview).toHaveBeenCalledWith("run-a", "artifact-run-a"));
    rerender(<NarrativeResults runId="run-b" />);
    await waitFor(() => expect(screen.getByText("Summary for run-b")).toBeInTheDocument());
    await act(async () => { oldPreview.resolve(preview("run-a")); });
    expect(screen.getByText("Summary for run-b")).toBeInTheDocument();
    expect(screen.queryByText("Summary for run-a")).not.toBeInTheDocument();
  });
});
