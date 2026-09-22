import { describe, expect, it } from "vitest";

import { makeComposition, makeValidationResult } from "@/test/composerFixtures";
import type { CompositionProposal, ValidationEntryDTO } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";

import {
  blockedVerbsFromReadiness,
  projectDecisionRows,
} from "./decisionPanelRows";

const S1: ValidationEntryDTO = {
  component: "pipeline",
  message:
    "Consider adding error routing to a retention output — failed rows are currently discarded rather than kept for review.",
  severity: "low",
};

function pendingEvent(overrides: Partial<InterpretationEvent> = {}): InterpretationEvent {
  return {
    id: "event-1",
    session_id: "session-1",
    composition_state_id: "state-1",
    affected_node_id: "colour_questions",
    tool_call_id: null,
    user_term: "llm_prompt_template:colour_questions",
    kind: "llm_prompt_template",
    llm_draft: "draft",
    choice: "pending",
    ...overrides,
  } as InterpretationEvent;
}

function proposal(overrides: Partial<CompositionProposal> = {}): CompositionProposal {
  return {
    id: "proposal-1",
    session_id: "session-1",
    tool_call_id: "call-1",
    tool_name: "patch_node_options",
    status: "pending",
    summary: "Change one option on colour_questions.",
    rationale: "Requested by the current composer turn.",
    affects: ["nodes"],
    arguments_redacted_json: {},
    base_state_id: null,
    committed_state_id: null,
    audit_event_id: null,
    created_at: "2026-09-13T11:16:03Z",
    updated_at: "2026-09-13T11:16:03Z",
    ...overrides,
  };
}

const empty = {
  compositionState: null,
  pendingInterpretations: [] as InterpretationEvent[],
  proposals: [] as CompositionProposal[],
};

describe("blockedVerbsFromReadiness", () => {
  // Derived from the readiness AXES, never from blocker codes: a new
  // backend blocker code must not need a client change to name what it
  // blocks.
  it("names nothing on a fully ready result", () => {
    expect(blockedVerbsFromReadiness(makeValidationResult().readiness)).toEqual([]);
  });

  it("names Save for review alone when only completion is withheld", () => {
    const readiness = {
      ...makeValidationResult().readiness,
      completion_ready: false,
    };
    expect(blockedVerbsFromReadiness(readiness)).toEqual(["save_for_review"]);
  });

  it("names Run and Save for review when execution is not ready", () => {
    const readiness = {
      ...makeValidationResult().readiness,
      execution_ready: false,
      completion_ready: false,
    };
    expect(blockedVerbsFromReadiness(readiness)).toEqual(["run", "save_for_review"]);
  });
});

describe("projectDecisionRows", () => {
  it("projects nothing when validation is green and nothing is pending", () => {
    const rows = projectDecisionRows({
      ...empty,
      validationResult: makeValidationResult(),
    });
    expect(rows.rows).toEqual([]);
    expect(rows.blockedVerbs).toEqual([]);
    expect(rows.count).toBe(0);
  });

  it("projects nothing before validation has run", () => {
    expect(projectDecisionRows({ ...empty, validationResult: null }).rows).toEqual([]);
  });

  it("projects the live 94f6f00c shape: completion withheld, one validator suggestion", () => {
    // The measured 2026-09-13 case: green preflight, advisor sign-off
    // withheld (completion_ready=false, execution_ready stays true), and
    // the validator's S1 nudge on the composition state.
    const validationResult = makeValidationResult({
      readiness: {
        authoring_valid: true,
        execution_ready: true,
        completion_ready: false,
        blockers: [
          {
            code: "advisor_signoff_blocked",
            component_id: "pipeline",
            suggestion: null,
            note: null,
            component_type: "pipeline",
            detail: "Completion advisory review did not clear after the available attempts.",
          },
        ],
      },
    });
    const projected = projectDecisionRows({
      ...empty,
      validationResult,
      compositionState: makeComposition(7, { validation_suggestions: [S1] }),
    });
    expect(projected.blockedVerbs).toEqual(["save_for_review"]);
    expect(projected.rows).toEqual([
      {
        kind: "blocker",
        suggestion: null,
        // elspeth-032ec69c41: carried from the wire blocker; null here, and
        // deliberately absent from the id above, which is unchanged.
        note: null,
        id: expect.stringContaining("blocker:"),
        code: "advisor_signoff_blocked",
        componentId: "pipeline",
        detail: "Completion advisory review did not clear after the available attempts.",
      },
      {
        kind: "suggestion",
        id: expect.stringContaining("suggestion:"),
        suggestion: S1,
      },
    ]);
    expect(projected.count).toBe(2);
  });

  it("keeps suggestions out of the panel when nothing is blocked", () => {
    // S1 fires on nearly every default-routed pipeline (a9887ded9); when
    // the user is not blocked it stays in the Checks tab, not in the chat.
    const projected = projectDecisionRows({
      ...empty,
      validationResult: makeValidationResult(),
      compositionState: makeComposition(1, { validation_suggestions: [S1] }),
    });
    expect(projected.rows).toEqual([]);
  });

  it("collapses the interpretation_review_pending blocker into the pending card pointers", () => {
    const validationResult = makeValidationResult({
      is_valid: false,
      readiness: {
        authoring_valid: true,
        execution_ready: false,
        completion_ready: false,
        blockers: [
          {
            code: "interpretation_review_pending",
            component_id: "colour_questions",
            suggestion: null,
            note: null,
            component_type: "transform",
            detail: "1 interpretation awaits review.",
          },
        ],
      },
    });
    const projected = projectDecisionRows({
      ...empty,
      validationResult,
      pendingInterpretations: [pendingEvent()],
    });
    expect(projected.rows).toEqual([
      {
        kind: "pending_interpretation",
        id: "interpretation:event-1",
        eventId: "event-1",
        userTerm: "llm_prompt_template:colour_questions",
        affectedNodeId: "colour_questions",
      },
    ]);
    expect(projected.blockedVerbs).toEqual(["run", "save_for_review"]);
  });

  it("keeps the interpretation blocker when no pending card exists to point at", () => {
    // An orphaned or not-yet-loaded review must still show as a blocker
    // rather than vanish because the pointer set is empty.
    const validationResult = makeValidationResult({
      readiness: {
        authoring_valid: true,
        execution_ready: false,
        completion_ready: false,
        blockers: [
          {
            code: "interpretation_review_pending",
            component_id: "colour_questions",
            suggestion: null,
            note: null,
            component_type: "transform",
            detail: "1 interpretation awaits review.",
          },
        ],
      },
    });
    const projected = projectDecisionRows({ ...empty, validationResult });
    expect(projected.rows.map((r) => r.kind)).toEqual(["blocker"]);
  });

  it("counts all pending proposals, including obsolete proposals that can be rejected", () => {
    const projected = projectDecisionRows({
      ...empty,
      validationResult: makeValidationResult(),
      proposals: [
        proposal(),
        proposal({ id: "proposal-2" }),
        proposal({ id: "proposal-3", status: "committed" }),
      ],
    });
    expect(projected.rows).toEqual([
      { kind: "pending_proposal", id: "proposal:proposal-1", proposalId: "proposal-1" },
      { kind: "pending_proposal", id: "proposal:proposal-2", proposalId: "proposal-2" },
    ]);
    expect(projected.count).toBe(2);
  });

  it("orders blockers first, then suggestions, then pointers, then proposals", () => {
    const validationResult = makeValidationResult({
      readiness: {
        authoring_valid: true,
        execution_ready: true,
        completion_ready: false,
        blockers: [
          {
            code: "advisor_signoff_blocked",
            component_id: "pipeline",
            suggestion: null,
            note: null,
            component_type: "pipeline",
            detail: "withheld",
          },
        ],
      },
    });
    const projected = projectDecisionRows({
      validationResult,
      compositionState: makeComposition(7, { validation_suggestions: [S1] }),
      pendingInterpretations: [pendingEvent()],
      proposals: [proposal()],
    });
    expect(projected.rows.map((r) => r.kind)).toEqual([
      "blocker",
      "suggestion",
      "pending_interpretation",
      "pending_proposal",
    ]);
    expect(projected.count).toBe(4);
  });
});

// Source fallback is a pending decision even before validation exists.
it("projects the eligible inline source candidate into the decision count", () => {
  const result = projectDecisionRows({ ...empty, validationResult: null, inlineSourceCandidate: "red, blue" });
  expect(result.rows).toEqual([{ kind: "inline_source_fallback", id: "inline-source:red, blue", candidateText: "red, blue" }]);
  expect(result.count).toBe(1);
});

it("tracks changed remedies and preserves identities under reorder", () => {
  const validationResult = makeValidationResult({readiness: {
    authoring_valid: true, execution_ready: true, completion_ready: false, blockers: [],
  }});
  const a = { component: "out", message: "First remedy", severity: "low" as const };
  const b = { component: "out", message: "Second remedy", severity: "low" as const };
  const project = (suggestions: ValidationEntryDTO[]) => projectDecisionRows({ ...empty, validationResult,
    compositionState: makeComposition(1, { validation_suggestions: suggestions }),
  }).rows.map((row) => row.id);
  expect(project([a])).not.toEqual(project([b]));
  expect(project([a, b])).toEqual(project([b, a]).reverse());
  expect(new Set(project([a, a])).size).toBe(2);
});
