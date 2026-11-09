// #352: POST /api/tutorial/run was refused with 409 "Session operation is
// already active" because the pre-run card's Run button was clickable while
// the build's last request still held the session fence: the final review
// resolve (COMPOSE until its POST returns — a post-commit refreshAll can drop
// the card before then) and the /validate the resolve's minted version fires
// (its surfacer repair pass can escalate to COMPOSE). These tests drive the
// REAL interpretation/session/execution stores and the real auto-validate
// queue through that sequence against stubbed endpoints, and pin that Run is
// disabled while either request is outstanding and enabled once both settle.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { _resetSubscriptionsForTesting, initStoreSubscriptions } from "@/stores/subscriptions";
import type { InterpretationEvent } from "@/types/interpretation";
import { TURN_4_READY_BODY, TURN_4_RUN_BUTTON, TURN_4_SETTLING_BODY } from "./copy";
import { abandonTutorialRun, TutorialTurn4Run } from "./TutorialTurn4Run";

vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));

const SESSION = "00000000-0000-0000-0000-000000000352";
const realExecutionValidate = useExecutionStore.getState().validate;
const realResolveEvent = useInterpretationEventsStore.getState().resolveEvent;

function composition(version: number) {
  return {
    id: `state-v${version}`,
    session_id: SESSION,
    version,
    sources: { source: { plugin: "text", options: { content: `hello ${version}` } } },
    nodes: [],
    edges: [],
    outputs: [],
  };
}

function reviewEvent(choice: InterpretationEvent["choice"]): InterpretationEvent {
  return {
    id: "evt-5",
    session_id: SESSION,
    composition_state_id: "state-v5",
    affected_node_id: "node-1",
    tool_call_id: "tool-1",
    user_term: "cool",
    kind: null,
    llm_draft: "interesting and engaging",
    accepted_value: choice === "pending" ? null : "interesting and engaging",
    choice,
    created_at: "2026-11-09T03:02:25Z",
    resolved_at: choice === "pending" ? null : "2026-11-09T03:02:28Z",
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
    approved_prompt_artifact_hash: null,
  };
}

function validationResult(executionReady: boolean) {
  return {
    is_valid: executionReady,
    checks: [],
    errors: [],
    warnings: [],
    readiness: {
      authoring_valid: true,
      execution_ready: executionReady,
      completion_ready: executionReady,
      blockers: [],
    },
  } as never;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 6; i += 1) await Promise.resolve();
  });
}

function runButton(): HTMLElement {
  return screen.getByRole("button", { name: TURN_4_RUN_BUTTON });
}

function renderRunCard() {
  return render(
    <TutorialTurn4Run sessionId={SESSION} onCompleted={vi.fn()} onCancelled={vi.fn()} />,
  );
}

describe("TutorialTurn4Run — Run waits for the session's in-flight operation (#352)", () => {
  let validatePipeline: ReturnType<typeof vi.spyOn>;
  let resolveInterpretation: ReturnType<typeof vi.spyOn>;
  let runTutorialPipeline: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    _resetSubscriptionsForTesting();
    vi.restoreAllMocks();
    useAuthStore.setState({ token: "tok", user: { user_id: "user-a" } as never });
    useInterpretationEventsStore.setState(useInterpretationEventsStore.getInitialState(), true);
    useInterpretationEventsStore.setState({
      resolveEvent: realResolveEvent,
      refreshAll: vi.fn().mockResolvedValue(undefined),
      pendingBySession: { [SESSION]: { "evt-5": reviewEvent("pending") } },
    } as never);
    useSessionStore.setState({
      activeSessionId: SESSION,
      compositionState: null,
      selectedNodeId: null,
      isComposing: false,
      proposalActionPendingIds: [],
      sessions: [{ id: SESSION, title: "First-run tutorial (in progress)" } as never],
      injectSystemMessage: vi.fn(),
    } as never);
    useExecutionStore.setState({
      validate: realExecutionValidate,
      isExecuting: false,
      isValidating: false,
      progress: null,
      validationResult: null,
    } as never);
    vi.spyOn(api, "fetchPluginPolicy").mockResolvedValue({
      data: { selections: [], control_modes: [] },
      snapshotFingerprint: "fp",
    } as never);
    runTutorialPipeline = vi.spyOn(api, "runTutorialPipeline").mockReturnValue(
      new Promise(() => {}) as never,
    );
    resolveInterpretation = vi.spyOn(api, "resolveInterpretation");
    validatePipeline = vi.spyOn(api, "validatePipeline");
    initStoreSubscriptions();

    // Seed v5: auto-validated and landed (the last review card still pending).
    validatePipeline.mockResolvedValueOnce(validationResult(false));
    useSessionStore.setState({ compositionState: composition(5) as never } as never);
    await waitFor(() => expect(validatePipeline).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(useExecutionStore.getState().isValidating).toBe(false));
    await flushMicrotasks();
    validatePipeline.mockClear();
  });

  afterEach(() => {
    // The run cache is module-level and keyed by session id: drop the entry a
    // clicked Run left behind so the next test mounts the pre-run card.
    vi.spyOn(api, "cancelTutorialRun").mockResolvedValue({ cancelled: false } as never);
    abandonTutorialRun(SESSION);
    _resetSubscriptionsForTesting();
  });

  it("disables Run while the last resolve and its /validate are in flight, then enables it", async () => {
    const resolveGate = deferred<never>();
    resolveInterpretation.mockReturnValueOnce(resolveGate.promise);
    const validateGate = deferred<never>();
    validatePipeline.mockReturnValueOnce(validateGate.promise);

    // The learner resolves the last card; the POST holds COMPOSE server-side.
    let resolution!: Promise<{ new_state: unknown }>;
    act(() => {
      resolution = useInterpretationEventsStore
        .getState()
        .resolveEvent(SESSION, "evt-5", { choice: "accepted_as_drafted" });
    });
    // A refreshAll serviced after the resolve committed drops the card before
    // the POST returns (the post-commit window interpretationEventsStore
    // documents). This is exactly when the pre-fix build gate (pending count
    // zero) let the learner through to an enabled Run.
    act(() => {
      useInterpretationEventsStore.setState((s) => ({
        pendingBySession: { ...s.pendingBySession, [SESSION]: {} },
      }));
    });
    expect(
      Object.keys(useInterpretationEventsStore.getState().pendingBySession[SESSION] ?? {}),
    ).toHaveLength(0);

    renderRunCard();
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(TURN_4_SETTLING_BODY)).toBeInTheDocument();
    fireEvent.click(runButton());
    expect(runTutorialPipeline).not.toHaveBeenCalled();

    // The resolve returns (server released its fence) and the card's
    // onResolved publishes the minted v6, which fires POST /validate at once.
    await act(async () => {
      resolveGate.resolve({
        event: reviewEvent("accepted_as_drafted"),
        new_state: composition(6),
      } as never);
      const { new_state } = await resolution;
      useSessionStore.getState().applyResolvedInterpretation(new_state as never);
    });
    expect(validatePipeline).toHaveBeenCalledTimes(1);
    expect(useExecutionStore.getState().isValidating).toBe(true);
    expect(runButton()).toBeDisabled();
    fireEvent.click(runButton());
    expect(runTutorialPipeline).not.toHaveBeenCalled();

    // /validate settles: nothing this client started is outstanding.
    await act(async () => {
      validateGate.resolve(validationResult(true));
    });
    await flushMicrotasks();
    await waitFor(() => expect(runButton()).toBeEnabled());
    expect(screen.getByText(TURN_4_READY_BODY)).toBeInTheDocument();

    fireEvent.click(runButton());
    expect(runTutorialPipeline).toHaveBeenCalledTimes(1);
  });

  it("a failed resolve also releases the gate (no stuck-disabled Run)", async () => {
    let rejectResolve!: (error: unknown) => void;
    resolveInterpretation.mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectResolve = reject;
      }) as never,
    );
    let resolution!: Promise<unknown>;
    act(() => {
      resolution = useInterpretationEventsStore
        .getState()
        .resolveEvent(SESSION, "evt-5", { choice: "accepted_as_drafted" })
        .catch(() => undefined);
    });
    renderRunCard();
    expect(runButton()).toBeDisabled();

    await act(async () => {
      rejectResolve(new Error("boom"));
      await resolution;
    });
    expect(runButton()).toBeEnabled();
  });

  it("a resolve in flight for ANOTHER session does not gate this session's Run", async () => {
    resolveInterpretation.mockReturnValueOnce(new Promise(() => {}) as never);
    act(() => {
      void useInterpretationEventsStore
        .getState()
        .resolveEvent("00000000-0000-0000-0000-00000000aaaa", "evt-x", {
          choice: "accepted_as_drafted",
        })
        .catch(() => undefined);
    });
    renderRunCard();
    expect(runButton()).toBeEnabled();
  });
});
