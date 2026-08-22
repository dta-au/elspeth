import { describe, it, expect, vi, beforeEach } from "vitest";
import { useExecutionStore } from "./executionStore";
import { useInterpretationEventsStore } from "./interpretationEventsStore";
import { useSessionStore } from "./sessionStore";
import { connectToRun } from "@/api/websocket";
import {
  EXECUTION_BLOCKED_VALIDATION_READINESS,
  makeValidationResult,
} from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type {
  Run,
  RunAccounting,
  RunDiagnostics,
  RunEvent,
  RunProgress,
  ValidationResult,
} from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";

// Mock the API client
vi.mock("@/api/client", () => ({
  validatePipeline: vi.fn(),
  executePipeline: vi.fn(),
  cancelRun: vi.fn(),
  createRunWebSocketTicket: vi.fn(),
  fetchRuns: vi.fn().mockResolvedValue([]),
  fetchRunDiagnostics: vi.fn(),
  evaluateRunDiagnostics: vi.fn(),
}));

vi.mock("@/api/websocket", () => ({
  connectToRun: vi.fn(),
}));

const READY_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
};
const BLOCKED_READINESS = {
  authoring_valid: false,
  execution_ready: false,
  completion_ready: false,
  blockers: [],
};

function resetInterpretationStore(): void {
  resetStore(useInterpretationEventsStore);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });

  return { promise, resolve, reject };
}

describe("executionStore.validate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
  });

  it("stores validation result on success", async () => {
    const result: ValidationResult = {
      is_valid: true,
      summary: "All checks passed",
      checks: [],
      errors: [],
      warnings: [],
      readiness: READY_READINESS,
    };

    const { validatePipeline } = await import("@/api/client");
    (validatePipeline as ReturnType<typeof vi.fn>).mockResolvedValue(result);

    await useExecutionStore.getState().validate("session-1");

    const state = useExecutionStore.getState();
    expect(validatePipeline).toHaveBeenCalledWith("session-1", "state-1");
    expect(state.validationResult).toEqual(result);
    expect(state.isValidating).toBe(false);
  });

  it("stores validation result on failure without side effects", async () => {
    const failedResult: ValidationResult = {
      is_valid: false,
      summary: "Validation failed",
      checks: [],
      errors: [
        {
          component_id: "llm_extract",
          component_type: "transform",
          message: "Missing required option: model",
          suggestion: "Add a model option",
        },
      ],
      warnings: [],
      readiness: {
        ...BLOCKED_READINESS,
        blockers: [
          {
            code: "settings_load",
            component_id: "llm_extract",
            component_type: "transform",
            detail: "llm_extract",
          },
        ],
      },
    };

    const { validatePipeline } = await import("@/api/client");
    (validatePipeline as ReturnType<typeof vi.fn>).mockResolvedValue(failedResult);

    await useExecutionStore.getState().validate("session-1");

    // validate() should only store the result — no cross-store side effects.
    // Orchestration (system messages, LLM feedback) is handled by subscriptions.
    const state = useExecutionStore.getState();
    expect(state.validationResult).toEqual(failedResult);
    expect(state.isValidating).toBe(false);
    expect(state.error).toBeNull();
  });

  it("blocks execution until the pending interpretation review is resolved", async () => {
    const { executePipeline } = await import("@/api/client");
    useInterpretationEventsStore.setState({
      pendingBySession: {
        "session-1": {
          "evt-1": makeInterpretationEvent({
            kind: "llm_model_choice",
            affected_node_id: "label_colors",
            llm_draft: "openai/gpt-4.1",
          }),
        },
      },
      resolvedCountBySession: {},
      resolvedBySession: {},
      optedOutBySession: {},
    });

    const runId = await useExecutionStore.getState().execute("session-1");

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(executePipeline).not.toHaveBeenCalled();
    expect(state.isExecuting).toBe(false);
    expect(state.error).toBe(
      "Set the LLM model choice for label_colors before running.",
    );
  });

  it("sets error state when API call fails", async () => {
    const { validatePipeline } = await import("@/api/client");
    (validatePipeline as ReturnType<typeof vi.fn>).mockRejectedValue({
      status: 500,
      detail: "Internal server error",
    });

    const result = await useExecutionStore.getState().validate("session-1");

    const state = useExecutionStore.getState();
    expect(state.validationResult).toBeNull();
    expect(state.isValidating).toBe(false);
    expect(state.error).toContain("internal error");
    expect(state.validationError).toBe(
      "Validation encountered an internal error. Please try again.",
    );
    // Catch path must return false so the caller does not cache this version.
    expect(result).toBe(false);
  });

  it("does not store a validation result that resolves after the user switches sessions", async () => {
    const staleResult: ValidationResult = {
      is_valid: true,
      summary: "Stale session result",
      checks: [],
      errors: [],
      warnings: [],
      readiness: READY_READINESS,
    };
    let resolveValidation: (result: ValidationResult) => void = () => {};
    const pendingValidation = new Promise<ValidationResult>((resolve) => {
      resolveValidation = resolve;
    });
    const { validatePipeline } = await import("@/api/client");
    (validatePipeline as ReturnType<typeof vi.fn>).mockReturnValue(pendingValidation);

    const validatePromise = useExecutionStore.getState().validate("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    resolveValidation(staleResult);
    await validatePromise;

    const state = useExecutionStore.getState();
    expect(state.validationResult).toBeNull();
    expect(state.isValidating).toBe(false);
  });

  it("does not store a validation result after the composition version changes", async () => {
    const staleResult: ValidationResult = {
      is_valid: false,
      summary: "Stale version result",
      checks: [],
      errors: [
        {
          component_id: "source",
          component_type: "source",
          message: "Missing path on the old snapshot",
          suggestion: null,
        },
      ],
      warnings: [],
      readiness: {
        ...BLOCKED_READINESS,
        blockers: [
          {
            code: "settings_load",
            component_id: "source",
            component_type: "source",
            detail: "source",
          },
        ],
      },
    };
    let resolveValidation: (result: ValidationResult) => void = () => {};
    const pendingValidation = new Promise<ValidationResult>((resolve) => {
      resolveValidation = resolve;
    });
    const { validatePipeline } = await import("@/api/client");
    (validatePipeline as ReturnType<typeof vi.fn>).mockReturnValue(pendingValidation);

    const validatePromise = useExecutionStore.getState().validate("session-1");
    useSessionStore.setState({
      compositionState: { version: 2, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
    resolveValidation(staleResult);
    const applied = await validatePromise;

    const state = useExecutionStore.getState();
    expect(applied).toBe(false);
    expect(state.validationResult).toBeNull();
    expect(state.isValidating).toBe(false);
  });
});

function makeRun(overrides: Partial<Run> & { error?: string | null } = {}): Run {
  return {
    id: "run-1",
    session_id: "session-1",
    status: "running",
    accounting: null,
    started_at: "2026-04-26T05:31:57.000Z",
    finished_at: null,
    composition_version: 1,
    ...overrides,
  } as Run;
}

function makeInterpretationEvent(
  overrides: Partial<InterpretationEvent> = {},
): InterpretationEvent {
  return {
    id: "evt-1",
    session_id: "session-1",
    composition_state_id: "state-1",
    affected_node_id: "llm_classify",
    tool_call_id: "tc-1",
    user_term: "cool",
    kind: "vague_term",
    llm_draft: "engaging",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-04-26T05:31:57.000Z",
    resolved_at: null,
    actor: "user:owner:u-1",
    interpretation_source: "user_approved",
    model_identifier: "anthropic/claude-opus-4-7",
    model_version: "20260518",
    provider: "anthropic",
    composer_skill_hash: "deadbeef",
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    resolved_prompt_template_hash: null,
    ...overrides,
  };
}

function makeAccounting(overrides: Partial<RunAccounting> = {}): RunAccounting {
  return {
    source: { rows_processed: 1, rows_rejected: 0, rows_read: 1 },
    sources: { source: { rows_processed: 1, rows_rejected: 0, rows_read: 1 } },
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
    ...overrides,
  };
}

describe("executionStore.execute", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
  });

  it("drops a late start success after the active session changes", async () => {
    const { executePipeline, fetchRuns } = await import("@/api/client");
    const pendingExecute = deferred<{ run_id: string }>();
    (executePipeline as ReturnType<typeof vi.fn>).mockReturnValue(
      pendingExecute.promise,
    );
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    const executePromise = useExecutionStore.getState().execute("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    pendingExecute.resolve({ run_id: "run-stale" });
    const runId = await executePromise;

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(state.activeRunId).toBeNull();
    expect(state.progress).toBeNull();
    expect(state.isExecuting).toBe(false);
    expect(fetchRuns).not.toHaveBeenCalled();
    expect(connectToRun).not.toHaveBeenCalled();
  });

  it("drops a late fanout guard after the active session changes", async () => {
    const { executePipeline } = await import("@/api/client");
    const pendingExecute = deferred<never>();
    const guard = {
      ack_token: "ack-line-explode",
      risk_level: "high" as const,
      summary: "LLM transform fanout needs confirmation.",
      risks: [],
    };
    (executePipeline as ReturnType<typeof vi.fn>).mockReturnValue(
      pendingExecute.promise,
    );

    const executePromise = useExecutionStore.getState().execute("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    pendingExecute.reject({
      status: 428,
      detail: guard.summary,
      error_type: "execution_fanout_ack_required",
      fanout_guard: guard,
    });
    const runId = await executePromise;

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(state.pendingFanoutGuard).toBeNull();
    expect(state.pendingFanoutSessionId).toBeNull();
    expect(state.error).toBeNull();
    expect(state.isExecuting).toBe(false);
  });

  it("drops a late execution error after the active session changes", async () => {
    const { executePipeline } = await import("@/api/client");
    const pendingExecute = deferred<never>();
    (executePipeline as ReturnType<typeof vi.fn>).mockReturnValue(
      pendingExecute.promise,
    );

    const executePromise = useExecutionStore.getState().execute("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    pendingExecute.reject({
      status: 500,
      detail: "old session failed",
    });
    const runId = await executePromise;

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(state.error).toBeNull();
    expect(state.isExecuting).toBe(false);
  });
});

function makeDiagnostics(overrides: Partial<RunDiagnostics> = {}): RunDiagnostics {
  return {
    run_id: "run-1",
    landscape_run_id: "run-1",
    run_status: "running",
    cancel_requested: false,
    summary: {
      token_count: 1,
      preview_limit: 50,
      preview_truncated: false,
      discard_count: 0,
      state_counts: { completed: 1 },
      operation_counts: { source_load: 1 },
      latest_activity_at: null,
    },
    tokens: [
      {
        token_id: "token-1",
        row_id: "row-1",
        row_index: 0,
        lineage: [],
        join_group_id: null,
        step_in_pipeline: null,
        created_at: "2026-04-26T05:31:58.000Z",
        terminal_outcome: "completed",
        states: [
          {
            state_id: "state-1",
            token_id: "token-1",
            node_id: "extract",
            step_index: 1,
            attempt: 0,
            status: "completed",
            duration_ms: 125,
            started_at: "2026-04-26T05:31:58.000Z",
            completed_at: "2026-04-26T05:31:59.000Z",
            error: null,
            success_reason: null,
          },
        ],
      },
    ],
    operations: [],
    artifacts: [],
    discards: [],
    failure_detail: null,
    ...overrides,
  };
}

describe("executionStore failed run events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
  });

  it("passes an authenticated ticket provider to the websocket client", async () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    const { createRunWebSocketTicket } = await import("@/api/client");
    (createRunWebSocketTicket as ReturnType<typeof vi.fn>).mockResolvedValue({
      ticket: "opaque-ws-ticket",
      expires_at: "2026-06-22T12:00:30.000Z",
    });

    useExecutionStore.getState().connectWebSocket("run-1");

    expect(connectToRun).toHaveBeenCalledTimes(1);
    expect(connectToRun).toHaveBeenCalledWith(
      "run-1",
      expect.any(Function),
      expect.objectContaining({
        onAuthFailure: expect.any(Function),
      }),
    );
    const ticketProvider = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][1] as () => Promise<string>;
    await expect(ticketProvider()).resolves.toBe("opaque-ws-ticket");
    expect(createRunWebSocketTicket).toHaveBeenCalledWith("run-1");
  });

  it("preserves terminal failed event detail in progress and run list", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];
    const failedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:31:58.000Z",
      event_type: "failed",
      data: {
        status: "failed",
        detail: "Pipeline execution failed (FrameworkBugError)",
        node_id: null,
      },
    };

    handlers.onFailed(failedEvent, failedEvent.data);

    const state = useExecutionStore.getState();
    expect(state.progress?.status).toBe("failed");
    expect(state.progress?.recent_errors[0]).toEqual({
      message: "Pipeline execution failed (FrameworkBugError)",
      node_id: null,
      row_id: null,
    });
    expect(state.runs[0]).toMatchObject({
      status: "failed",
      error: "Pipeline execution failed (FrameworkBugError)",
    });
  });
});

describe("executionStore fanout guard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
  });

  const guard = {
    ack_token: "ack-line-explode",
    risk_level: "high" as const,
    summary: "LLM transform 'classify_line' may make an unknown number of OpenRouter calls.",
    risks: [
      {
        node_id: "classify_line",
        provider: "openrouter",
        model: "openai/gpt-4o-mini",
        credential_ref: "secret_ref:OPENROUTER_API_KEY",
        estimated_provider_calls: null,
        provider_calls_per_row: 1,
        upstream_fanout: ["transform:explode_lines:line_explode"],
        risk_level: "high" as const,
        message: "LLM transform 'classify_line' may make one OpenRouter call per expanded row.",
      },
    ],
  };

  it("holds a 428 fanout guard for explicit user confirmation", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValue({
      status: 428,
      detail: guard.summary,
      error_type: "execution_fanout_ack_required",
      fanout_guard: guard,
    });

    const runId = await useExecutionStore.getState().execute("session-1");

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(executePipeline).toHaveBeenCalledWith(
      "session-1",
      undefined,
      "state-1",
    );
    expect(state.pendingFanoutGuard).toEqual(guard);
    expect(state.pendingFanoutSessionId).toBe("session-1");
    expect(state.isExecuting).toBe(false);
    expect(state.error).toBeNull();
  });

  it("retries execution with the accepted fanout guard token", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce({
        status: 428,
        detail: guard.summary,
        error_type: "execution_fanout_ack_required",
        fanout_guard: guard,
      })
      .mockResolvedValueOnce({ run_id: "run-1" });

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");
    const runId = await useExecutionStore.getState().confirmFanoutExecution();

    const state = useExecutionStore.getState();
    expect(runId).toBe("run-1");
    expect(executePipeline).toHaveBeenLastCalledWith(
      "session-1",
      {
        accepted: true,
        token: "ack-line-explode",
      },
      "state-1",
    );
    expect(state.pendingFanoutGuard).toBeNull();
    expect(state.pendingFanoutSessionId).toBeNull();
    expect(state.activeRunId).toBe("run-1");
  });

  it("settles a stale fanout guard without executing when live readiness is false", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
      status: 428,
      detail: guard.summary,
      error_type: "execution_fanout_ack_required",
      fanout_guard: guard,
    });

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");
    useExecutionStore.getState().setValidationResult(
      makeValidationResult({
        readiness: EXECUTION_BLOCKED_VALIDATION_READINESS,
      }),
    );

    const runId = await useExecutionStore.getState().confirmFanoutExecution();

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(executePipeline).toHaveBeenCalledTimes(1);
    expect(state.pendingFanoutGuard).toBeNull();
    expect(state.pendingFanoutSessionId).toBeNull();
    expect(state.isExecuting).toBe(false);
    expect(state.error).toBe(
      "This pipeline is no longer ready to run. Validate it again before executing.",
    );
  });

  it("settles a stale fanout guard without executing when readiness is missing", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
      status: 428,
      detail: guard.summary,
      error_type: "execution_fanout_ack_required",
      fanout_guard: guard,
    });

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");
    useExecutionStore.getState().setValidationResult({
      is_valid: true,
      checks: [],
      errors: [],
      warnings: [],
    } as unknown as ValidationResult);

    const runId = await useExecutionStore.getState().confirmFanoutExecution();

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(executePipeline).toHaveBeenCalledTimes(1);
    expect(state.pendingFanoutGuard).toBeNull();
    expect(state.pendingFanoutSessionId).toBeNull();
    expect(state.isExecuting).toBe(false);
    expect(state.error).toBe(
      "This pipeline is no longer ready to run. Validate it again before executing.",
    );
  });

  it("settles the guard synchronously at dispatch so the dialog closes atomically (elspeth-8363555f05)", async () => {
    const { executePipeline } = await import("@/api/client");
    let resolveDispatch: (value: { run_id: string }) => void = () => {};
    (executePipeline as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce({
        status: 428,
        detail: guard.summary,
        error_type: "execution_fanout_ack_required",
        fanout_guard: guard,
      })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveDispatch = resolve;
          }),
      );

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");

    const confirmPromise = useExecutionStore.getState().confirmFanoutExecution();

    // BEFORE the dispatch resolves: the guard is already settled, so the
    // ConfirmDialog (mounted solely off pendingFanoutGuard) is gone — no
    // second Execute activation, and no Cancel affordance that would read
    // as a successful cancellation while the request is in flight.
    const inFlight = useExecutionStore.getState();
    expect(inFlight.pendingFanoutGuard).toBeNull();
    expect(inFlight.pendingFanoutSessionId).toBeNull();

    resolveDispatch({ run_id: "run-atomic" });
    expect(await confirmPromise).toBe("run-atomic");
    expect(useExecutionStore.getState().activeRunId).toBe("run-atomic");
  });

  it("makes a second confirmation during dispatch a no-op instead of a duplicate run", async () => {
    const { executePipeline } = await import("@/api/client");
    let resolveDispatch: (value: { run_id: string }) => void = () => {};
    (executePipeline as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce({
        status: 428,
        detail: guard.summary,
        error_type: "execution_fanout_ack_required",
        fanout_guard: guard,
      })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveDispatch = resolve;
          }),
      );

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");

    const first = useExecutionStore.getState().confirmFanoutExecution();
    const second = useExecutionStore.getState().confirmFanoutExecution();

    expect(await second).toBeNull();
    resolveDispatch({ run_id: "run-once" });
    expect(await first).toBe("run-once");
    // 1 initial 428 + exactly 1 acknowledged dispatch — never a duplicate.
    expect(executePipeline).toHaveBeenCalledTimes(2);
  });

  it("restores the guard when the acknowledged dispatch itself returns a fresh 428", async () => {
    const { executePipeline } = await import("@/api/client");
    const freshGuard = { ...guard, ack_token: "ack-rotated" };
    (executePipeline as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce({
        status: 428,
        detail: guard.summary,
        error_type: "execution_fanout_ack_required",
        fanout_guard: guard,
      })
      .mockRejectedValueOnce({
        status: 428,
        detail: freshGuard.summary,
        error_type: "execution_fanout_ack_required",
        fanout_guard: freshGuard,
      });

    useExecutionStore.setState({ validationResult: makeValidationResult() });
    await useExecutionStore.getState().execute("session-1");
    const runId = await useExecutionStore.getState().confirmFanoutExecution();

    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    // Atomic settle must not dead-end a rotated token: the fresh guard
    // re-arms the dialog for a new explicit confirmation.
    expect(state.pendingFanoutGuard).toEqual(freshGuard);
    expect(state.pendingFanoutSessionId).toBe("session-1");
  });
});

describe("executionStore.cancel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
  });

  it("marks an active run as cancelling while the backend drains work", async () => {
    const { cancelRun } = await import("@/api/client");
    (cancelRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: "running",
      cancel_requested: true,
    });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
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
      },
    });

    await useExecutionStore.getState().cancel("run-1");

    const state = useExecutionStore.getState();
    expect(state.runs[0].cancel_requested).toBe(true);
    expect(state.progress?.cancel_requested).toBe(true);
    // Nothing terminal happened, so no outcome may be recorded — otherwise
    // the toast and badge would fire on a run that is still draining.
    expect(state.lastRunOutcome).toBeNull();
  });

  // Cancelling a not-yet-started run takes the backend's non-Event branch:
  // status flips to "cancelled" in the response with NO run-event broadcast.
  // Writing that into progress without recording the outcome strands the run
  // twice — the toast waits on the WS 60s idle recheck, AND the loadRuns
  // degraded-path reconciliation is disarmed by the very write (it is gated
  // on progress NOT already being terminal). Net effect before this guard:
  // no Run-tab badge at all and a toast up to a minute late.
  it("records the terminal outcome when the response cancels a not-yet-started run", async () => {
    const { cancelRun } = await import("@/api/client");
    (cancelRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: "cancelled",
      cancel_requested: false,
    });
    useExecutionStore.setState({
      runs: [makeRun({ status: "pending" })],
      activeRunId: "run-1",
      activeRunSessionId: "session-launch",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        cancel_requested: false,
        accounting: null,
        recent_errors: [],
        status: "pending",
      },
    });
    // A session switch after launch must not re-route the outcome: the
    // stamp, not the session store, is the routing key.
    useSessionStore.setState({ activeSessionId: "session-other" } as never);

    await useExecutionStore.getState().cancel("run-1");

    const state = useExecutionStore.getState();
    expect(state.progress?.status).toBe("cancelled");
    expect(state.lastRunOutcome).toEqual({
      runId: "run-1",
      status: "cancelled",
      sessionId: "session-launch",
    });
  });

  it("does not record an outcome for a cancelled run this tab does not own", async () => {
    const { cancelRun } = await import("@/api/client");
    (cancelRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: "cancelled",
      cancel_requested: false,
    });
    useExecutionStore.setState({
      runs: [makeRun({ id: "run-other", status: "pending" })],
      activeRunId: "run-1",
      activeRunSessionId: "session-launch",
      progress: null,
    });

    await useExecutionStore.getState().cancel("run-other");

    const state = useExecutionStore.getState();
    expect(state.runs[0].status).toBe("cancelled");
    // The always-mounted surfaces speak for the ACTIVE run only.
    expect(state.lastRunOutcome).toBeNull();
  });
});

describe("executionStore WebSocket lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
  });

  it("marks the active run disconnected during reconnect and clears it after the socket recovers", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    handlers.onDisconnected();
    expect(useExecutionStore.getState().wsDisconnected).toBe(true);

    handlers.onConnected();
    expect(useExecutionStore.getState().wsDisconnected).toBe(false);

    handlers.onDisconnected();
    const progressEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 3,
        tokens_succeeded: 2,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 1,
        tokens_routed_failure: 0,
      },
    };
    handlers.onProgress(progressEvent, progressEvent.data);
    expect(useExecutionStore.getState().wsDisconnected).toBe(false);
  });

  it("surfaces run-unavailable websocket closes without leaving reconnect state active", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      wsDisconnected: true,
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    handlers.onRunUnavailable();

    expect(useExecutionStore.getState().wsDisconnected).toBe(false);
    expect(useExecutionStore.getState().error).toBe(
      "Run is unavailable or you do not have access.",
    );
  });
});

describe("executionStore.loadRuns", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({ activeSessionId: "session-1" } as never);
  });

  it("returns loaded after applying the current session's run list", async () => {
    const { fetchRuns } = await import("@/api/client");
    const currentRuns = [makeRun({ id: "run-current" })];
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue(currentRuns);

    const outcome = await useExecutionStore.getState().loadRuns("session-1");

    expect(outcome).toBe("loaded");
    expect(useExecutionStore.getState().runs).toEqual(currentRuns);
  });

  it("drops a run-list response that resolves after the active session changes", async () => {
    const { fetchRuns } = await import("@/api/client");
    const pendingRuns = deferred<Run[]>();
    (fetchRuns as ReturnType<typeof vi.fn>).mockReturnValue(pendingRuns.promise);
    const currentRun = makeRun({
      id: "run-session-2",
      session_id: "session-2",
    });
    useExecutionStore.setState({ runs: [currentRun] });

    const loadPromise = useExecutionStore.getState().loadRuns("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    pendingRuns.resolve([
      makeRun({
        id: "run-stale-session-1",
        session_id: "session-1",
      }),
    ]);
    const outcome = await loadPromise;

    expect(outcome).toBe("stale");
    expect(useExecutionStore.getState().runs).toEqual([currentRun]);
  });

  it("returns unavailable without discarding cached runs when fetching fails", async () => {
    const { fetchRuns } = await import("@/api/client");
    const cachedRun = makeRun({ id: "run-cached" });
    useExecutionStore.setState({ runs: [cachedRun] });
    (fetchRuns as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("network down"),
    );

    const outcome = await useExecutionStore.getState().loadRuns("session-1");

    expect(outcome).toBe("unavailable");
    expect(useExecutionStore.getState().runs).toEqual([cachedRun]);
  });
});

// Reload-resilience (elspeth-90db33baac): a page reload during a running
// pipeline used to lose activeRunId + the WebSocket, leaving the run
// uncancellable from the UI. rehydrateActiveRun (fired from
// stores/subscriptions.ts on session activation) must reattach both.
describe("executionStore.rehydrateActiveRun", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({ activeSessionId: "session-1" } as never);
  });

  it("restores activeRunId, seeds progress, and reconnects the WebSocket when a running run exists", async () => {
    const { fetchRuns } = await import("@/api/client");
    const liveRun = makeRun({ id: "run-live", status: "running" });
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-old", status: "completed" }),
      liveRun,
    ]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    const state = useExecutionStore.getState();
    expect(state.runs).toHaveLength(2);
    expect(state.activeRunId).toBe("run-live");
    expect(state.progress?.status).toBe("running");
    expect(state.progress?.cancel_requested).toBe(false);
    expect(connectToRun).toHaveBeenCalledTimes(1);
    expect(connectToRun).toHaveBeenCalledWith(
      "run-live",
      expect.any(Function),
      expect.any(Object),
    );
  });

  // The launch-session stamp has TWO write sites and only execute()'s was
  // covered. rehydrate is the page-reload-during-a-live-run path — precisely
  // where a terminal WS event arrives later — so an unstamped rehydrate makes
  // applyRunEvent copy sessionId: null into lastRunOutcome, RunOutcomeNotice
  // dispatch {tab:"run", sessionId:null}, and ArtifactWorkspace DROP the
  // intent as a session mismatch: "View run" silently no-ops while still
  // acknowledging the toast away.
  it("stamps the rehydrated session as the run's launch session", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-live", status: "running" }),
    ]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    expect(useExecutionStore.getState().activeRunSessionId).toBe("session-1");
  });

  it("routes a terminal event after rehydration to the rehydrated session, not null", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-live", status: "running" }),
    ]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    // rehydrateActiveRun opens the socket itself; take the handlers it
    // registered rather than re-connecting, so this exercises the real
    // reload path end to end.
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock
      .calls[0][2] as Record<
      string,
      (event: RunEvent, data: RunEvent["data"]) => void
    >;
    const completedEvent: RunEvent = {
      run_id: "run-live",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "completed",
      data: {
        status: "completed",
        accounting: makeAccounting(),
        landscape_run_id: "landscape-run-1",
      },
    };

    handlers.onComplete(completedEvent, completedEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toEqual({
      runId: "run-live",
      status: "completed",
      sessionId: "session-1",
    });
  });

  it("also reattaches a queued (pending) run", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-queued", status: "pending" }),
    ]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    expect(useExecutionStore.getState().activeRunId).toBe("run-queued");
    expect(useExecutionStore.getState().progress?.status).toBe("pending");
    expect(connectToRun).toHaveBeenCalledWith(
      "run-queued",
      expect.any(Function),
      expect.any(Object),
    );
  });

  it("does not connect when every run is terminal", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-done", status: "completed" }),
      makeRun({ id: "run-bad", status: "failed" }),
    ]);

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    const state = useExecutionStore.getState();
    expect(state.runs).toHaveLength(2);
    expect(state.activeRunId).toBeNull();
    expect(state.progress).toBeNull();
    expect(connectToRun).not.toHaveBeenCalled();
  });

  it("does not stomp a run that execute() already owns", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-live", status: "running" }),
    ]);
    useExecutionStore.setState({ activeRunId: "run-live" });

    await useExecutionStore.getState().rehydrateActiveRun("session-1");

    expect(connectToRun).not.toHaveBeenCalled();
  });

  it("drops a response that resolves after the active session changes", async () => {
    const { fetchRuns } = await import("@/api/client");
    const pendingRuns = deferred<Run[]>();
    (fetchRuns as ReturnType<typeof vi.fn>).mockReturnValue(pendingRuns.promise);

    const rehydratePromise = useExecutionStore
      .getState()
      .rehydrateActiveRun("session-1");
    useSessionStore.setState({ activeSessionId: "session-2" } as never);
    pendingRuns.resolve([makeRun({ id: "run-live", status: "running" })]);
    await rehydratePromise;

    expect(useExecutionStore.getState().activeRunId).toBeNull();
    expect(connectToRun).not.toHaveBeenCalled();
  });

  it("swallows fetch failures (non-critical, polling retries)", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("network down"),
    );

    await expect(
      useExecutionStore.getState().rehydrateActiveRun("session-1"),
    ).resolves.toBeUndefined();
    expect(useExecutionStore.getState().activeRunId).toBeNull();
  });
});

// Pre-run egress disclosure opt-out (elspeth-c18ad229cc): per-session,
// in-memory, survives reset() (which fires on every session switch) and is
// flushed only by clearRunDisclosureAcks (auth identity change).
describe("executionStore run-disclosure acknowledgements", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().clearRunDisclosureAcks();
    useExecutionStore.getState().reset();
  });

  it("records an acknowledgement per session and preserves it across reset()", () => {
    useExecutionStore.getState().acknowledgeRunDisclosure("sess-1");
    expect(
      useExecutionStore.getState().runDisclosureAckBySession["sess-1"],
    ).toBe(true);
    expect(
      useExecutionStore.getState().runDisclosureAckBySession["sess-2"],
    ).toBeUndefined();

    useExecutionStore.getState().reset();
    expect(
      useExecutionStore.getState().runDisclosureAckBySession["sess-1"],
    ).toBe(true);
  });

  it("clearRunDisclosureAcks flushes every acknowledgement", () => {
    useExecutionStore.getState().acknowledgeRunDisclosure("sess-1");
    useExecutionStore.getState().acknowledgeRunDisclosure("sess-2");
    useExecutionStore.getState().clearRunDisclosureAcks();
    expect(useExecutionStore.getState().runDisclosureAckBySession).toEqual({});
  });
});

// Terminal-outcome surfacing (elspeth-3a7b7c7b37): lastRunOutcome is the
// only run-lifecycle fact readable by always-mounted surfaces (the
// RunOutcomeNotice toast, the Run-tab badge). It must carry the backend's
// VERBATIM terminal status, only for the run this tab owns.
describe("executionStore lastRunOutcome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({ activeSessionId: "session-1" } as never);
  });

  type RunEventHandler = (event: RunEvent, data: RunEvent["data"]) => void;

  // Seeds the post-launch state execute() itself writes: activeRunId AND the
  // launch session stamp that travels with it. The stamp is the ONLY session
  // an outcome may carry — the tests below never let the session store's
  // activeSessionId stand in for it.
  function seedActiveRun(
    launchSessionId: string = "session-1",
  ): Record<string, RunEventHandler> {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      activeRunSessionId: launchSessionId,
      progress: {
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
      },
    });
    useExecutionStore.getState().connectWebSocket("run-1");
    return (connectToRun as ReturnType<typeof vi.fn>).mock
      .calls[0][2] as Record<string, RunEventHandler>;
  }

  it("records the verbatim backend status for a completed_with_failures run", () => {
    const handlers = seedActiveRun();
    const completedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "completed",
      data: {
        status: "completed_with_failures",
        accounting: makeAccounting(),
        landscape_run_id: "landscape-run-1",
      },
    };

    handlers.onComplete(completedEvent, completedEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toEqual({
      runId: "run-1",
      status: "completed_with_failures",
      sessionId: "session-1",
    });
  });

  it("records a FAILED terminal outcome for the active run", () => {
    const handlers = seedActiveRun();
    const failedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "failed",
      data: {
        status: "failed",
        detail: "Pipeline execution failed (FrameworkBugError)",
        node_id: null,
      },
    };

    handlers.onFailed(failedEvent, failedEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toEqual({
      runId: "run-1",
      status: "failed",
      sessionId: "session-1",
    });
  });

  it("records a cancelled terminal outcome for the active run", () => {
    const handlers = seedActiveRun();
    const cancelledEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "cancelled",
      data: {
        status: "cancelled",
        source_rows_processed: 1,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
      },
    };

    handlers.onCancelled(cancelledEvent, cancelledEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toEqual({
      runId: "run-1",
      status: "cancelled",
      sessionId: "session-1",
    });
  });

  it("does not record an outcome from a terminal event for a non-active run", () => {
    const handlers = seedActiveRun();
    const staleEvent: RunEvent = {
      run_id: "run-other",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "failed",
      data: { status: "failed", detail: "stale", node_id: null },
    };

    handlers.onFailed(staleEvent, staleEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });

  it("does not record an outcome from non-terminal progress events", () => {
    const handlers = seedActiveRun();
    const progressEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 3,
        tokens_succeeded: 2,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
      },
    };

    handlers.onProgress(progressEvent, progressEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });

  it("acknowledgeRunOutcome clears the outcome", () => {
    useExecutionStore.setState({
      lastRunOutcome: { runId: "run-1", status: "completed", sessionId: "session-1" },
    });

    useExecutionStore.getState().acknowledgeRunOutcome();

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });

  it("reset clears the outcome", () => {
    useExecutionStore.setState({
      lastRunOutcome: { runId: "run-1", status: "failed", sessionId: "session-1" },
    });

    useExecutionStore.getState().reset();

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });

  it("a fresh launch supersedes an unacknowledged outcome", async () => {
    const { executePipeline, fetchRuns } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: "run-2",
    });
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
    useExecutionStore.setState({
      lastRunOutcome: { runId: "run-1", status: "failed", sessionId: "session-1" },
    });

    const runId = await useExecutionStore.getState().execute("session-1");

    expect(runId).toBe("run-2");
    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });

  // The session-switch window (elspeth-3a7b7c7b37): a session switch commits
  // useSessionStore.activeSessionId BEFORE useSession's passive effect calls
  // reset(), so a WS terminal event can land while activeSessionId already
  // names the NEW session but the store still owns the OLD run. Reading the
  // session store at event time mis-files the outcome under the session the
  // user just moved to, where RunOutcomeNotice would surface a stranger's
  // run. The launch stamp is the only attribution that survives that window.
  it("stamps the LAUNCH session, not the session active when the terminal event lands", () => {
    const handlers = seedActiveRun("session-A");
    useSessionStore.setState({ activeSessionId: "session-B" } as never);
    const completedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "completed",
      data: {
        status: "completed",
        accounting: makeAccounting(),
        landscape_run_id: "landscape-run-1",
      },
    };

    handlers.onComplete(completedEvent, completedEvent.data);

    expect(useExecutionStore.getState().lastRunOutcome).toEqual({
      runId: "run-1",
      status: "completed",
      sessionId: "session-A",
    });
  });

  it("execute stamps the launching session onto activeRunSessionId", async () => {
    const { executePipeline, fetchRuns } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: "run-2",
    });
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);

    await useExecutionStore.getState().execute("session-1");

    expect(useExecutionStore.getState().activeRunSessionId).toBe("session-1");
  });

  it("reset clears the launch session stamp with the run", () => {
    useExecutionStore.setState({
      activeRunId: "run-1",
      activeRunSessionId: "session-1",
    });

    useExecutionStore.getState().reset();

    expect(useExecutionStore.getState().activeRunId).toBeNull();
    expect(useExecutionStore.getState().activeRunSessionId).toBeNull();
  });
});

// Degraded-path reconciliation (elspeth-3a7b7c7b37): when the WebSocket drops
// its terminal event, the 3s loadRuns poll is the only remaining evidence the
// run finished. loadRuns must convert that evidence into the same two facts
// the WS branch writes — a recorded outcome and a reconciled progress.status —
// and must do so ONLY for the run this tab owns.
describe("executionStore loadRuns degraded-path reconciliation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
    useSessionStore.setState({ activeSessionId: "session-1" } as never);
  });

  function inFlightProgress(): RunProgress {
    return {
      source_rows_processed: 2,
      tokens_succeeded: 1,
      tokens_failed: 0,
      tokens_quarantined: 0,
      tokens_routed_success: 0,
      tokens_routed_failure: 0,
      cancel_requested: true,
      accounting: null,
      recent_errors: [],
      status: "running",
    };
  }

  it("records the outcome and reconciles progress when the poll finds the active run terminal", async () => {
    const { fetchRuns } = await import("@/api/client");
    const terminalRow = makeRun({
      status: "completed_with_failures",
      accounting: makeAccounting(),
      finished_at: "2026-04-26T05:32:08.000Z",
    });
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([terminalRow]);
    useExecutionStore.setState({
      activeRunId: "run-1",
      activeRunSessionId: "session-1",
      progress: inFlightProgress(),
    });

    const outcome = await useExecutionStore.getState().loadRuns("session-1");

    expect(outcome).toBe("loaded");
    const state = useExecutionStore.getState();
    // Verbatim backend status, stamped with the LAUNCH session — the same
    // contract the WS terminal branch writes.
    expect(state.lastRunOutcome).toEqual({
      runId: "run-1",
      status: "completed_with_failures",
      sessionId: "session-1",
    });
    // ProgressView must stop claiming a live run, and a cancel request that
    // can no longer be honoured must not survive the terminal transition.
    expect(state.progress?.status).toBe("completed_with_failures");
    expect(state.progress?.cancel_requested).toBe(false);
    expect(state.progress?.accounting).toEqual(terminalRow.accounting);
  });

  it("stamps the launch session on the degraded outcome, not the currently active session", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ status: "failed", finished_at: "2026-04-26T05:32:08.000Z" }),
    ]);
    useSessionStore.setState({ activeSessionId: "session-B" } as never);
    useExecutionStore.setState({
      activeRunId: "run-1",
      activeRunSessionId: "session-A",
      progress: inFlightProgress(),
    });

    await useExecutionStore.getState().loadRuns("session-B");

    expect(useExecutionStore.getState().lastRunOutcome?.sessionId).toBe(
      "session-A",
    );
  });

  it("does not reconcile a terminal row for a run this tab is not attached to", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ id: "run-other", status: "failed" }),
      makeRun({ id: "run-1", status: "running" }),
    ]);
    useExecutionStore.setState({
      activeRunId: "run-1",
      activeRunSessionId: "session-1",
      progress: inFlightProgress(),
    });

    await useExecutionStore.getState().loadRuns("session-1");

    const state = useExecutionStore.getState();
    expect(state.lastRunOutcome).toBeNull();
    expect(state.progress?.status).toBe("running");
    expect(state.runs).toHaveLength(2);
  });

  it("does not re-record an outcome once progress already reads terminal", async () => {
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({ status: "completed", finished_at: "2026-04-26T05:32:08.000Z" }),
    ]);
    useExecutionStore.setState({
      activeRunId: "run-1",
      activeRunSessionId: "session-1",
      progress: { ...inFlightProgress(), status: "completed", cancel_requested: false },
      // The WS branch already recorded and the operator already acknowledged.
      lastRunOutcome: null,
    });

    await useExecutionStore.getState().loadRuns("session-1");

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
  });
});

describe("executionStore progress events advance live accounting", () => {
  // The API run record no longer carries best-known live counters. Progress
  // events update state.progress, while completed events attach closed
  // accounting to the matching run record.
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetInterpretationStore();
  });

  it("advances live source/token counters without reintroducing legacy run rows", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    const firstProgress: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 50,
        tokens_succeeded: 40,
        tokens_failed: 1,
        tokens_quarantined: 0,
        tokens_routed_success: 7,
        tokens_routed_failure: 2,
      },
    };
    handlers.onProgress(firstProgress, firstProgress.data);

    let state = useExecutionStore.getState();
    expect(state.runs[0]).not.toHaveProperty("rows_processed");
    expect(state.runs[0]).toMatchObject({
      id: "run-1",
      status: "running",
      accounting: null,
      finished_at: null,
    });
    expect(state.progress).toMatchObject({
      source_rows_processed: 50,
      tokens_succeeded: 40,
      tokens_failed: 1,
      tokens_routed_success: 7,
      tokens_routed_failure: 2,
    });

    const secondProgress: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:05.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 120,
        tokens_succeeded: 95,
        tokens_failed: 3,
        tokens_quarantined: 0,
        tokens_routed_success: 18,
        tokens_routed_failure: 4,
      },
    };
    handlers.onProgress(secondProgress, secondProgress.data);

    state = useExecutionStore.getState();
    expect(state.progress).toMatchObject({
      source_rows_processed: 120,
      tokens_succeeded: 95,
      tokens_failed: 3,
      tokens_routed_success: 18,
      tokens_routed_failure: 4,
    });
  });

  it("leaves state.runs unchanged when progress arrives for an unknown run_id", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    const otherRun = makeRun({
      id: "run-other",
    });
    useExecutionStore.setState({
      runs: [otherRun],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    const event: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 99,
        tokens_succeeded: 72,
        tokens_failed: 9,
        tokens_quarantined: 0,
        tokens_routed_success: 9,
        tokens_routed_failure: 9,
      },
    };
    handlers.onProgress(event, event.data);

    const state = useExecutionStore.getState();
    expect(state.runs).toHaveLength(1);
    expect(state.runs[0]).toEqual(otherRun);
  });

  it("ignores progress events whose run_id does not match the active run", () => {
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 5,
        tokens_succeeded: 4,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 2,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    const staleEvent: RunEvent = {
      run_id: "run-old",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "progress",
      data: {
        source_rows_processed: 99,
        tokens_succeeded: 72,
        tokens_failed: 9,
        tokens_quarantined: 1,
        tokens_routed_success: 9,
        tokens_routed_failure: 9,
      },
    };
    handlers.onProgress(staleEvent, staleEvent.data);

    expect(useExecutionStore.getState().progress).toMatchObject({
      source_rows_processed: 5,
      tokens_succeeded: 4,
      tokens_failed: 0,
      tokens_quarantined: 0,
      tokens_routed_success: 2,
      tokens_routed_failure: 0,
    });
  });

  it("does not zero state.runs[i] counters on error events with null progress", () => {
    // RunEventError carries no accounting. The store must preserve the REST
    // snapshot instead of fabricating zeros during reconnect-before-progress.
    const close = vi.fn();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    const restSnapshot = makeRun({ accounting: makeAccounting() });
    useExecutionStore.setState({
      runs: [restSnapshot],
      activeRunId: "run-1",
      progress: null,
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    const errorEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:00.000Z",
      event_type: "error",
      data: {
        message: "Row-level transform exception",
        node_id: "extract",
        row_id: "row-7",
      },
    };
    handlers.onError(errorEvent, errorEvent.data);

    const state = useExecutionStore.getState();
    expect(state.runs[0]).toMatchObject({
      accounting: makeAccounting(),
    });
  });

  it("stores completed event accounting on progress and matching run", () => {
    const close = vi.fn();
    const accounting = makeAccounting();
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 1,
        tokens_succeeded: 9_000,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];

    const completedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "completed",
      data: {
        status: "completed",
        accounting,
        landscape_run_id: "landscape-run-1",
      },
    };
    handlers.onComplete(completedEvent, completedEvent.data);

    const state = useExecutionStore.getState();
    expect(state.progress).toMatchObject({
      source_rows_processed: 1,
      tokens_succeeded: 9_323,
      tokens_failed: 0,
      accounting,
      status: "completed",
    });
    expect(state.runs[0]).toMatchObject({
      status: "completed",
      accounting,
      finished_at: "2026-04-26T05:32:08.000Z",
    });
  });

  it("refreshes the run list after terminal completion so discard summaries reach the UI", async () => {
    const close = vi.fn();
    const accounting = makeAccounting({
      // Reconciles with the discard_summary below (validation_errors: 2) —
      // the backend rejects the contradictory shape.
      source: { rows_processed: 0, rows_rejected: 2, rows_read: 2 },
      tokens: {
        emitted: 0,
        terminal: 0,
        succeeded: 0,
        failed: 0,
        structural: 0,
        pending: 0,
        abandoned: 0,
      },
    });
    (connectToRun as ReturnType<typeof vi.fn>).mockReturnValue({ close });
    const { fetchRuns } = await import("@/api/client");
    (fetchRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeRun({
        id: "run-1",
        status: "empty",
        accounting,
        discard_summary: {
          total: 2,
          validation_errors: 2,
          transform_errors: 0,
          gate_errors: 0,
          sink_discards: 0,
          stages: [
            {
              stage: "source_validation",
              node_id: "source_csv_upload",
              count: 2,
            },
          ],
        },
      }),
    ]);
    useSessionStore.setState({ activeSessionId: "session-1" } as never);
    useExecutionStore.setState({
      runs: [makeRun()],
      activeRunId: "run-1",
      progress: {
        source_rows_processed: 0,
        tokens_succeeded: 0,
        tokens_failed: 0,
        tokens_quarantined: 0,
        tokens_routed_success: 0,
        tokens_routed_failure: 0,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    });

    useExecutionStore.getState().connectWebSocket("run-1");
    const handlers = (connectToRun as ReturnType<typeof vi.fn>).mock.calls[0][2];
    const completedEvent: RunEvent = {
      run_id: "run-1",
      timestamp: "2026-04-26T05:32:08.000Z",
      event_type: "completed",
      data: {
        status: "empty",
        accounting,
        landscape_run_id: "landscape-run-1",
      },
    };
    handlers.onComplete(completedEvent, completedEvent.data);

    expect(fetchRuns).toHaveBeenCalledWith("session-1");
  });
});

describe("executionStore run diagnostics", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
  });

  it("stores diagnostics snapshots by run id", async () => {
    const diagnostics = makeDiagnostics();
    const { fetchRunDiagnostics } = await import("@/api/client");
    (fetchRunDiagnostics as ReturnType<typeof vi.fn>).mockResolvedValue(diagnostics);

    await useExecutionStore.getState().loadRunDiagnostics("run-1");

    const state = useExecutionStore.getState();
    expect(state.diagnosticsByRunId["run-1"]).toEqual(diagnostics);
    expect(state.diagnosticsLoadingByRunId["run-1"]).toBe(false);
    expect(state.diagnosticsErrorByRunId["run-1"]).toBeNull();
  });

  it("stores LLM diagnostics explanations by run id", async () => {
    const { evaluateRunDiagnostics } = await import("@/api/client");
    (evaluateRunDiagnostics as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: "run-1",
      generated_at: "2026-04-26T05:32:00.000Z",
      explanation: "The run is still working and has loaded one token.",
      working_view: {
        headline: "The run is processing data",
        evidence: ["1 token is visible in the runtime trace."],
        meaning: "The run is still working and has loaded one token.",
        next_steps: ["Refresh diagnostics if this does not change soon."],
      },
    });

    await useExecutionStore.getState().evaluateRunDiagnostics("run-1");

    const state = useExecutionStore.getState();
    expect(state.diagnosticsExplanationByRunId["run-1"]).toContain("still working");
    expect(state.diagnosticsWorkingViewByRunId["run-1"]).toEqual({
      headline: "The run is processing data",
      evidence: ["1 token is visible in the runtime trace."],
      meaning: "The run is still working and has loaded one token.",
      next_steps: ["Refresh diagnostics if this does not change soon."],
    });
    expect(state.diagnosticsEvaluatingByRunId["run-1"]).toBe(false);
  });
});
