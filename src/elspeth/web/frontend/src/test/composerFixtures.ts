import type {
  CompositionState,
  ValidationError,
  ValidationReadiness,
  ValidationResult,
} from "../types/index";

export const READY_VALIDATION_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
} satisfies ValidationReadiness;

export const EXECUTION_BLOCKED_VALIDATION_READINESS = {
  authoring_valid: true,
  execution_ready: false,
  completion_ready: false,
  blockers: [
    {
      code: "runtime_admission",
      component_id: "pipeline",
      component_type: "pipeline",
      detail: "The selected runtime policy does not admit this pipeline.",
    },
  ],
} satisfies ValidationReadiness;

export const COMPLETION_BLOCKED_VALIDATION_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: false,
  blockers: [
    {
      code: "advisor_signoff_required",
      component_id: null,
      component_type: null,
      detail: "Advisor sign-off is required before sharing for review.",
    },
  ],
} satisfies ValidationReadiness;

export const INVALID_VALIDATION_READINESS = {
  authoring_valid: false,
  execution_ready: false,
  completion_ready: false,
  blockers: [
    {
      code: "validation_error",
      component_id: "node1",
      component_type: "transform",
      detail: "The transform did not pass validation.",
    },
  ],
} satisfies ValidationReadiness;

interface ValidationResultOverrides {
  is_valid?: boolean;
  errors?: ValidationError[];
  readiness?: ValidationReadiness;
}

/**
 * Canonical typed validation fixture.
 *
 * Readiness is mandatory in the backend response contract. Tests that need
 * malformed wire data must cast that value explicitly at the individual
 * boundary instead of weakening every ordinary fixture with `as never`.
 */
export function makeValidationResult(
  overrides: ValidationResultOverrides = {},
): ValidationResult {
  return {
    is_valid: true,
    checks: [],
    errors: [],
    warnings: [],
    readiness: READY_VALIDATION_READINESS,
    ...overrides,
  };
}

export const compositionStateAuthorityFields = {
  session_id: "session-1",
  is_valid: true,
  validation_errors: null,
  validation_warnings: null,
  validation_suggestions: null,
  derived_from_state_id: null,
  created_at: "2026-07-19T00:00:00Z",
  composer_meta: null,
  plugin_policy_findings: [],
} satisfies Pick<
  CompositionState,
  | "session_id"
  | "is_valid"
  | "validation_errors"
  | "validation_warnings"
  | "validation_suggestions"
  | "derived_from_state_id"
  | "created_at"
  | "composer_meta"
  | "plugin_policy_findings"
>;

/**
 * Canonical test fixture for CompositionState.
 *
 * NodeSpec arity (frontend `types/index.ts`):
 *   Required (7): id, node_type, plugin, input, on_success, on_error, options
 *   Optional (6): condition, routes, fork_to, branches, policy, merge
 * This is the frontend contract the fixture mirrors. The Python backend has 13
 * fields but the TypeScript interface marks 6 of them as optional — no `as never`
 * cast is needed once all required fields are supplied.
 *
 * Import from here in all test files that need CompositionState scaffolding.
 * Do NOT duplicate this fixture in individual test files.
 */
export function makeComposition(
  version: number,
  overrides?: Partial<CompositionState>,
): CompositionState {
  return {
    id: "comp-1",
    ...compositionStateAuthorityFields,
    version,
    sources: { source: { plugin: "csv_file", options: { path: "x.csv" } } },
    nodes: [
      {
        id: "select_columns",
        node_type: "transform",
        plugin: "select_columns",
        input: "source",
        on_success: null,
        on_error: null,
        options: {},
      },
    ],
    edges: [],
    outputs: [],
    metadata: { name: "demo", description: "" },
    ...overrides,
  };
}

/**
 * Returns a Promise that resolves to `value` after `delay` ms — UNLESS the
 * provided AbortSignal aborts first, in which case it rejects with a
 * synthetic AbortError matching the shape the production store's catch arm
 * checks (`err.name === "AbortError"`).
 *
 * Use this in tests that exercise the store's abort-stale-in-flight or
 * clearSession-aborts-controllers contracts; do NOT hand-roll a Promise
 * that ignores the signal — that forces components to paper over the gap
 * with synchronous setState (see elspeth-f018ea84c6).
 */
export function makeAbortablePromise<T>(
  value: T,
  options?: { delay?: number; signal?: AbortSignal },
): Promise<T> {
  const { delay = 0, signal } = options ?? {};
  return new Promise<T>((resolve, reject) => {
    const reject_with_abort = () => {
      const err = new Error("Aborted");
      err.name = "AbortError";
      reject(err);
    };
    if (signal?.aborted) {
      reject_with_abort();
      return;
    }
    const timer = setTimeout(() => resolve(value), delay);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject_with_abort();
    });
  });
}
