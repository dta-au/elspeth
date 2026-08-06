// ============================================================================
// completionOutcome -- the shared honest-completion-label derivation
// (elspeth-bf9c296ee5).
//
// Pinned contracts:
//   1. deriveCompletionOutcome ladder: an un-mutated turn is "response_ready"
//      no matter what else holds (the badge describes the turn, and an
//      answer-only turn claims nothing about the pipeline); a mutated turn is
//      "review_required" while pending decisions block the run gate,
//      "pipeline_ready" only when the backend admitted execution, and
//      "pipeline_updated" otherwise. The ladder can only ever UNDER-claim.
//   2. The label vocabulary is exactly the four-state taxonomy the UX review
//      required: Response ready / Pipeline updated / Review required /
//      Pipeline ready.
//   3. useCompletionOutcome mirrors ExecuteButton's gate inputs EXACTLY:
//      reviewPending is `!optedOut && pendingCount > 0` over the SAME
//      pendingBySession map (unfiltered — every pending row blocks Run, so
//      every pending row must block a "ready" claim), and executionReady is
//      `validationResult?.readiness?.execution_ready === true`. A label that
//      claims ready while ExecuteButton refuses to run is the exact defect
//      this module exists to prevent.
// ============================================================================

import { beforeEach, describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  COMPLETION_OUTCOME_LABELS,
  deriveCompletionOutcome,
  useCompletionOutcome,
} from "./completionOutcome";
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import type { InterpretationEvent } from "@/types/interpretation";
import type { ValidationResult } from "@/types/index";

const READY_VALIDATION = {
  is_valid: true,
  checks: [],
  errors: [],
  warnings: [],
  readiness: {
    authoring_valid: true,
    execution_ready: true,
    completion_ready: true,
    blockers: [],
  },
} as unknown as ValidationResult;

const NOT_READY_VALIDATION = {
  is_valid: true,
  checks: [],
  errors: [],
  warnings: [],
  readiness: {
    authoring_valid: true,
    execution_ready: false,
    completion_ready: false,
    blockers: [],
  },
} as unknown as ValidationResult;

// Only key-presence matters to the gate predicate; the value shape is
// irrelevant to the count, so a minimal stand-in suffices.
const PENDING_EVENT = { id: "evt-1", choice: "pending" } as InterpretationEvent;

describe("deriveCompletionOutcome -- the honesty ladder", () => {
  it("an un-mutated turn is response_ready regardless of ambient state", () => {
    // The badge describes the turn: an answer-only completion never claims a
    // pipeline update or readiness, even when the ambient pipeline happens to
    // be ready or blocked.
    for (const reviewPending of [false, true]) {
      for (const executionReady of [false, true]) {
        expect(
          deriveCompletionOutcome({
            pipelineMutated: false,
            reviewPending,
            executionReady,
          }),
        ).toBe("response_ready");
      }
    }
  });

  it("pending review blocks a mutated turn from claiming ready", () => {
    expect(
      deriveCompletionOutcome({
        pipelineMutated: true,
        reviewPending: true,
        executionReady: false,
      }),
    ).toBe("review_required");
    // Pending review wins even over backend admission — the run gate refuses
    // to run in that state, so the label must not claim ready.
    expect(
      deriveCompletionOutcome({
        pipelineMutated: true,
        reviewPending: true,
        executionReady: true,
      }),
    ).toBe("review_required");
  });

  it("pipeline_ready requires backend execution admission", () => {
    expect(
      deriveCompletionOutcome({
        pipelineMutated: true,
        reviewPending: false,
        executionReady: true,
      }),
    ).toBe("pipeline_ready");
  });

  it("a mutated, unblocked, un-admitted pipeline is pipeline_updated", () => {
    expect(
      deriveCompletionOutcome({
        pipelineMutated: true,
        reviewPending: false,
        executionReady: false,
      }),
    ).toBe("pipeline_updated");
  });
});

describe("COMPLETION_OUTCOME_LABELS -- the four-state taxonomy", () => {
  it("carries exactly the labels the terminal copy must distinguish", () => {
    expect(COMPLETION_OUTCOME_LABELS).toEqual({
      response_ready: "Response ready",
      pipeline_updated: "Pipeline updated",
      review_required: "Review required",
      pipeline_ready: "Pipeline ready",
    });
  });
});

describe("useCompletionOutcome -- ExecuteButton gate mirror", () => {
  beforeEach(() => {
    resetStore(useExecutionStore);
    resetStore(useInterpretationEventsStore);
  });

  it("defaults to pipeline_updated: no pending rows, no validation verdict", () => {
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("pipeline_updated");
  });

  it("a pending interpretation row forces review_required", () => {
    useInterpretationEventsStore.setState({
      pendingBySession: { "sess-1": { "evt-1": PENDING_EVENT } },
    });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("review_required");
  });

  it("a pending row in ANOTHER session does not block this one", () => {
    useInterpretationEventsStore.setState({
      pendingBySession: { "sess-other": { "evt-1": PENDING_EVENT } },
    });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("pipeline_updated");
  });

  it("session opt-out unblocks review, mirroring the run gate's complement", () => {
    useInterpretationEventsStore.setState({
      pendingBySession: { "sess-1": { "evt-1": PENDING_EVENT } },
      optedOutBySession: { "sess-1": true },
    });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("pipeline_updated");
  });

  it("backend execution admission yields pipeline_ready", () => {
    useExecutionStore.setState({ validationResult: READY_VALIDATION });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("pipeline_ready");
  });

  it("a validation verdict WITHOUT execution admission stays pipeline_updated", () => {
    useExecutionStore.setState({ validationResult: NOT_READY_VALIDATION });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("pipeline_updated");
  });

  it("pending review beats execution admission, exactly like the run gate", () => {
    useExecutionStore.setState({ validationResult: READY_VALIDATION });
    useInterpretationEventsStore.setState({
      pendingBySession: { "sess-1": { "evt-1": PENDING_EVENT } },
    });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", true));
    expect(result.current).toBe("review_required");
  });

  it("an un-mutated turn is response_ready even when the pipeline is admitted", () => {
    useExecutionStore.setState({ validationResult: READY_VALIDATION });
    const { result } = renderHook(() => useCompletionOutcome("sess-1", false));
    expect(result.current).toBe("response_ready");
  });

  it("a null session id derives from bare signals (no pending, no verdict)", () => {
    const { result } = renderHook(() => useCompletionOutcome(null, true));
    expect(result.current).toBe("pipeline_updated");
  });
});
