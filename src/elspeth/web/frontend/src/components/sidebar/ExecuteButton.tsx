import { useCallback, useEffect, useId, useState } from "react";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useAuditReadinessStore } from "@/stores/auditReadinessStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { Button, Input } from "@/components/ui";
import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import type {
  CompositionState,
  NodeSpec,
  PluginSummary,
  SourceSpec,
} from "@/types/index";
import type { ReadinessRowId } from "@/types/api";
import {
  REQUEST_RUN_EVENT,
  dispatchArtifactViewIntent,
} from "@/lib/composer-events";

/**
 * Run-button tooltip text used when a pending interpretation event blocks
 * execution. Exported for the corresponding test so the assertion is pinned
 * against the exact string the spec table requires (18b-phase-5b-frontend.md
 * line 705: `title="Resolve pending interpretation first."`).
 */
export const INTERPRETATION_PENDING_RUN_BLOCK_TITLE =
  "Resolve pending interpretation first.";

/**
 * Fallback transform plugins treated as network-reaching when the plugin
 * catalog hasn't loaded yet (so `PluginSummary.audit_characteristics` isn't
 * available to classify by). This set predates the catalog-driven check
 * below and is Azure-only by history, not by design — it under-discloses
 * the AWS externals (aws_textract_document_analysis,
 * aws_bedrock_content_safety, aws_bedrock_prompt_shield, all backend-side
 * `Determinism.EXTERNAL_CALL`) once R2-F7 (elspeth-27bc704359) surfaced the
 * gap. Kept ONLY as the no-catalog fallback; whenever the catalog has
 * loaded, `audit_characteristics` (below) is the sole source of truth so
 * this list can never drift from the backend's own classification again.
 */
const NETWORK_FETCH_PLUGINS = new Set([
  "web_scrape",
  "azure_content_safety",
  "azure_prompt_shield",
  "azure_document_intelligence",
]);

/** True if `pluginName` is classified as reaching the network, per the
 *  catalog's `audit_characteristics` for that transform.
 *
 *  Fail-open by design for a plugin name absent from a *loaded* catalog
 *  (e.g. a saved composition referencing a plugin since renamed or
 *  policy-revoked): `.some()` over no match returns `false`, so an
 *  unrecognised plugin is silently treated as non-network rather than
 *  flagged. This differs from the catalog-*failed-to-load* case in
 *  `buildRunEgressSummary`, which surfaces an explicit uncertainty line
 *  instead of guessing. A stale-but-present catalog entry is assumed
 *  accurate; if that assumption stops holding (e.g. plugin renames
 *  routinely leave orphaned references in saved compositions) this should
 *  fail closed the same way the load-failure path does. */
function catalogFlagsExternalCall(
  pluginName: string,
  catalogTransforms: readonly PluginSummary[],
): boolean {
  return catalogTransforms.some(
    (summary) =>
      summary.name === pluginName &&
      summary.audit_characteristics.includes("external_call"),
  );
}

/** True if `node` is already disclosed via the "Sends rows to the
 *  configured LLM" line in `buildRunEgressSummary`. Used there to keep the
 *  network-egress classification (and its catalog-load-failure uncertainty
 *  note) from re-listing the same node under a second line.
 *
 *  Assumption this depends on: no `Determinism.EXTERNAL_CALL` transform
 *  today declares a `model` option or has "llm" in its plugin name, so LLM
 *  nodes and network-reaching transforms are disjoint sets. If a future
 *  plugin is both (e.g. a hosted LLM-gateway transform also flagged
 *  `external_call`), it would only ever appear on the LLM line, not the
 *  network line or the load-failure uncertainty line below — revisit this
 *  helper if that happens. */
function isLlmNode(node: NodeSpec): boolean {
  return (
    typeof node.options?.model === "string" ||
    (node.plugin ?? "").includes("llm")
  );
}

const SAFE_LLM_PROFILE_ALIAS = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/;
const SAFE_LLM_MODEL_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$/;

function hasOwnOption(options: Record<string, unknown>, name: string): boolean {
  return Object.prototype.hasOwnProperty.call(options, name);
}

/** Return only a safe, author-visible LLM binding label.
 *
 * Web profile lowering deliberately keeps the opaque authored `profile`
 * alias in composition state while provider, model, endpoint, and credential
 * bindings stay operator-private. If profile provenance is present but
 * malformed, fail closed to the generic label instead of falling through to
 * a possibly resolved private model. `profile_alias` and `resolved_model`
 * are executable/audit provenance, never consent-dialog display values.
 */
function llmSourceBindingLabel(source: SourceSpec): string {
  const { options } = source;
  if (hasOwnOption(options, "profile")) {
    const profile = options.profile;
    if (typeof profile === "string" && SAFE_LLM_PROFILE_ALIAS.test(profile)) {
      return `profile ${profile}`;
    }
    return "configured LLM";
  }
  if (
    hasOwnOption(options, "profile_alias") ||
    hasOwnOption(options, "resolved_model")
  ) {
    return "configured LLM";
  }
  const model = options.model;
  if (typeof model === "string" && SAFE_LLM_MODEL_IDENTIFIER.test(model)) {
    return `model ${model}`;
  }
  return "configured LLM";
}

function catalogFlagsLlmSource(
  source: SourceSpec,
  catalogSources: readonly PluginSummary[] | null,
  catalogCurrent: boolean,
): boolean {
  if (source.plugin === "llm") return true;
  if (!catalogCurrent || catalogSources === null) return false;
  return catalogSources.some(
    (summary) =>
      summary.plugin_type === "source" &&
      summary.name === source.plugin &&
      summary.capability_tags.includes("llm"),
  );
}

/**
 * Derive the pre-run egress disclosure lines from the actual composition
 * (elspeth-c18ad229cc). Each line names configured components from the live
 * CompositionState; catalog metadata classifies their external effects.
 *
 * `catalogTransforms` is the plugin catalog's transform list
 * (`usePluginCatalogStore((s) => s.transforms)`), the same source of truth
 * the audit-readiness `plugin_trust` row and
 * `tests/unit/web/audit_readiness/test_boundary_predicate_parity.py` are
 * pinned against (R2-F7, elspeth-27bc704359) — a transform is network-
 * reaching here iff its backend-declared `audit_characteristics` contains
 * `"external_call"`. `catalogTransforms === null` is ambiguous by itself —
 * it means either "hasn't loaded yet" (transient; `catalogLoadFailed`
 * false) or "failed to load" (`catalogLoadFailed` true, from
 * `pluginCatalogStore.error`) — because the store resets `transforms` to
 * `null` in both cases and only `error`/`isLoading` carry the
 * discriminator. The two are handled differently:
 *   - not yet loaded: silently fall back to the hardcoded
 *     `NETWORK_FETCH_PLUGINS` set, same as before catalog-driven
 *     classification existed — expected to resolve before the user reaches
 *     this dialog.
 *   - failed to load: still use the hardcoded set for the confident line,
 *     but any other configured transform cannot be verified either way, so
 *     it is named in an explicit uncertainty line rather than silently
 *     assumed safe — the exact under-disclosure R2-F7 fixed must not
 *     reappear whenever a catalog fetch errors.
 *
 * `catalogSources` classifies source plugins carrying the catalog's `llm`
 * capability tag. Only a settled catalog is authoritative. The exact built-in
 * `llm` identity remains a bounded fallback while the catalog is loading,
 * failed, stale, or missing that entry, so its authored prompt is never
 * silently omitted from consent.
 * Exported for tests.
 */
export function buildRunEgressSummary(
  compositionState: CompositionState | null,
  catalogTransforms: readonly PluginSummary[] | null = null,
  catalogLoadFailed = false,
  catalogSources: readonly PluginSummary[] | null = null,
  catalogIsLoading = false,
): string[] {
  if (!compositionState) return [];
  const lines: string[] = [];

  const sourceEntries = sortedSourceEntries(compositionState);
  const catalogCurrent =
    catalogSources !== null && !catalogLoadFailed && !catalogIsLoading;
  const llmSourceEntries = sourceEntries.filter(([, source]) =>
    catalogFlagsLlmSource(source, catalogSources, catalogCurrent),
  );
  const ordinarySources = sourceEntries
    .filter(([, source]) =>
      !catalogFlagsLlmSource(source, catalogSources, catalogCurrent),
    )
    .map(
      ([sourceName, source]) =>
        `${sourceComponentId(sourceName)} (${source.plugin})`,
    );
  if (ordinarySources.length > 0) {
    lines.push(`Reads source data: ${ordinarySources.join(", ")}.`);
  }

  const llmSources = llmSourceEntries.map(
    ([sourceName, source]) =>
      `${sourceComponentId(sourceName)} (${llmSourceBindingLabel(source)})`,
  );
  if (llmSources.length > 0) {
    lines.push(
      `Sends one authored prompt to the configured LLM: ${llmSources.join(", ")}.`,
    );
  }

  // `?.` on options: display-only derivation that must tolerate partially
  // formed compositions mid-authoring rather than crash the Run button.
  const llmNodes = compositionState.nodes
    .filter(isLlmNode)
    .map((node) =>
      typeof node.options?.model === "string"
        ? `${node.id} (model ${node.options.model})`
        : node.id,
    );
  if (llmNodes.length > 0) {
    lines.push(`Sends rows to the configured LLM: ${llmNodes.join(", ")}.`);
  }

  let networkNodes: string[];
  let unverifiableNodes: string[] = [];

  if (catalogTransforms !== null) {
    // Catalog loaded — audit_characteristics is authoritative.
    networkNodes = compositionState.nodes
      .filter(
        (node) =>
          node.plugin !== null &&
          catalogFlagsExternalCall(node.plugin, catalogTransforms),
      )
      .map((node) => `${node.id} (${node.plugin})`);
  } else if (catalogLoadFailed) {
    // Catalog failed to load — the hardcoded set still yields a confident
    // (if incomplete) line, but every other configured, non-LLM transform
    // is genuinely unverifiable: silently omitting it here would reproduce
    // R2-F7's under-disclosure, so it is named explicitly instead.
    const nonLlmTransforms = compositionState.nodes.filter(
      (node) => node.plugin !== null && !isLlmNode(node),
    );
    networkNodes = nonLlmTransforms
      .filter((node) => NETWORK_FETCH_PLUGINS.has(node.plugin as string))
      .map((node) => `${node.id} (${node.plugin})`);
    unverifiableNodes = nonLlmTransforms
      .filter((node) => !NETWORK_FETCH_PLUGINS.has(node.plugin as string))
      .map((node) => `${node.id} (${node.plugin})`);
  } else {
    // Not loaded yet (transient) — same fallback behaviour as before
    // catalog-driven classification existed.
    networkNodes = compositionState.nodes
      .filter(
        (node) =>
          node.plugin !== null && NETWORK_FETCH_PLUGINS.has(node.plugin),
      )
      .map((node) => `${node.id} (${node.plugin})`);
  }

  if (networkNodes.length > 0) {
    lines.push(`Fetches over the network: ${networkNodes.join(", ")}.`);
  }
  if (unverifiableNodes.length > 0) {
    lines.push(
      `Plugin catalog unavailable — external-service effects could not be ` +
        `fully enumerated for: ${unverifiableNodes.join(", ")}.`,
    );
  }

  const outputs = compositionState.outputs.map(
    (output) => `${output.name} (${output.plugin})`,
  );
  if (outputs.length > 0) {
    lines.push(`Writes output: ${outputs.join(", ")}.`);
  }

  return lines;
}

/**
 * Which audit-readiness rows are load-bearing for `canExecute` below, and
 * which are informational/advisory only (elspeth-088bf83922 finding T-2,
 * option (a) — legibility, NOT new gating). Read together with
 * `canExecute`: `validationResult?.readiness?.execution_ready === true`
 * corresponds to the backend-owned execution admission reported through the
 * `validation` row; `!isRunBlocked` corresponds to the `llm_interpretations`
 * row (both are driven by the same interpretationEventsStore pending/
 * opted-out state used to compute `isRunBlocked` below). The other four
 * rows (plugin_trust, provenance, retention, secrets) never appear in
 * `canExecute` and are always advisory.
 *
 * This helper is the single source of truth for what actually gates Run.
 * The live and shared panel parents use it to prepare each row's explicit
 * `blocksRun` value from backend execution readiness; AuditReadinessRow only
 * renders that prepared fact. The exhaustive switch fails the build if a
 * future backend row id lacks an explicit classification.
 */
export function isRunGatingReadinessRow(
  id: ReadinessRowId,
  validationExecutionReady: boolean,
): boolean {
  switch (id) {
    case "validation":
      return !validationExecutionReady;
    case "llm_interpretations":
      return true;
    case "plugin_trust":
    case "provenance":
    case "retention":
    case "secrets":
      return false;
    default: {
      const _exhaustive: never = id;
      throw new Error(`unknown readiness row id: ${String(_exhaustive)}`);
    }
  }
}

/** Which of the (up to) three run-blocking gates is currently active, for
 *  the plain-language reason text rendered under the button
 *  (elspeth-088bf83922 T-2). Priority order: an in-flight run takes
 *  precedence (nothing else matters until it finishes); pending
 *  interpretation review is next (it also drives the dedicated
 *  aria-disabled/title/aria-describedby treatment below); then no validation
 *  result, structural validation failure, and backend execution-readiness
 *  refusal. Returns null when none apply, i.e. when `canExecute` is true.
 *  Exported for the corresponding test. */
export type RunBlockReason = "running" | "interpretation" | "validation" | "readiness" | "not_validated";

export function primaryRunBlockReason(input: {
  isExecuting: boolean;
  progressRunning: boolean;
  isRunBlocked: boolean;
  validationFailing: boolean;
  executionReadinessBlocked: boolean;
  validationNotRun: boolean;
}): RunBlockReason | null {
  if (input.isExecuting || input.progressRunning) return "running";
  if (input.isRunBlocked) return "interpretation";
  // "not_validated" (no validation result yet — empty composition, or a
  // snapshot still in flight) is distinct from "validation" (a result exists
  // and reports errors). Conflating them made the button claim "fix the
  // validation errors" when none had been computed and none were shown
  // anywhere (elspeth-088bf83922 review follow-up).
  if (input.validationNotRun) return "not_validated";
  if (input.validationFailing) return "validation";
  if (input.executionReadinessBlocked) return "readiness";
  return null;
}

/** Plain-language text per `RunBlockReason`, rendered visibly below the
 *  button. The `interpretation` entry reuses
 *  INTERPRETATION_PENDING_RUN_BLOCK_TITLE verbatim rather than restating it
 *  — one string, two channels (the existing title/aria-describedby pair for
 *  mouse/some-AT users, this visible line for everyone else). */
const RUN_BLOCK_REASON_TEXT: Record<RunBlockReason, string> = {
  running: "The pipeline is already running.",
  interpretation: INTERPRETATION_PENDING_RUN_BLOCK_TITLE,
  validation: "Fix the validation errors shown in the Audit panel before running.",
  readiness: "The backend has not admitted this pipeline for execution.",
  not_validated: "This pipeline hasn't been validated yet.",
};

/**
 * Run-pipeline button (Phase 2C, with Phase 5b.18b.7 interpretation gating).
 *
 * Gating predicate (spec 18b lines 698-722):
 *
 *   isRunBlocked = !optedOut && Object.keys(pendingBySession[sessionId] ?? {}).length > 0
 *
 * When `isRunBlocked` is true the button:
 *   - stays in the tab order (NO native `disabled` — that would remove the
 *     button from the tab order and make the blocked reason unreachable for
 *     exactly the keyboard/screen-reader users it exists for, WCAG 4.1.2 /
 *     elspeth-94c32de486),
 *   - sets `aria-disabled="true"` so AT users hear the disabled state,
 *   - no-ops in onClick,
 *   - sets `title` to the spec-required tooltip string,
 *   - sets `aria-describedby` pointing to a visually-hidden span with the
 *     same text so screen readers receive the affordance text (a
 *     `title` attribute alone is not reliably announced).
 *
 * Other not-runnable states (not yet validated, structural validation
 * failing, backend execution readiness blocked, already executing/running)
 * keep native `disabled` — the WCAG 4.1.2 concern above is specific to the
 * interpretation block, which is the only state that removes reachability
 * from a mouseless/AT user if left natively disabled. They still get a
 * plain-language reason: see `primaryRunBlockReason` below.
 *
 * The opt-out path is the gate's complement: a session that has opted out
 * of interpretation review runs freely (the backend bakes auto-
 * interpretations directly into prompt templates and records them as
 * `interpretation_source='auto_interpreted_opt_out'` in the audit trail).
 *
 * Egress disclosure (elspeth-c18ad229cc): a runnable click first opens a
 * ConfirmDialog summarising what the run will reach (derived from the live
 * composition), mirroring the tutorial's Run-step disclosure. A per-session
 * "don't ask again" opt-out (executionStore.runDisclosureAckBySession)
 * keeps it from becoming click-through noise.
 *
 * Gate legibility (elspeth-088bf83922 T-2, option (a)): the audit-readiness
 * panel's rows other than validation/llm_interpretations never block Run —
 * this button previously gave no hint of that distinction. `canExecute`
 * explicitly gates on backend execution readiness, interpretation review,
 * and active-run state; the visible surfaces make those decisions legible:
 *   - when disabled, a one-line reason for an active run, pending
 *     interpretation, missing validation, structural validation failure, or
 *     backend readiness refusal renders below the button, driven by
 *     `primaryRunBlockReason`;
 *   - when enabled but the audit snapshot has a non-green advisory row
 *     (plugin_trust/provenance/retention/secrets), a single line notes
 *     that advisory checks don't block Run.
 */
export function ExecuteButton(): JSX.Element | null {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const compositionState = useSessionStore((s) => s.compositionState);
  const validationResult = useExecutionStore((s) => s.validationResult);
  const isExecuting = useExecutionStore((s) => s.isExecuting);
  const progress = useExecutionStore((s) => s.progress);
  const execute = useExecutionStore((s) => s.execute);
  const disclosureAcknowledged = useExecutionStore((s) =>
    activeSessionId ? s.runDisclosureAckBySession[activeSessionId] === true : false,
  );
  const acknowledgeRunDisclosure = useExecutionStore(
    (s) => s.acknowledgeRunDisclosure,
  );

  // Phase 5b.18b.7 — interpretation-review run-button gating. Subscribe to
  // the per-session sub-maps (not the whole store state) so the button
  // re-renders precisely when its inputs change.
  const pendingInterpretationsBySession = useInterpretationEventsStore(
    (s) => s.pendingBySession,
  );
  const optedOutInterpretationsBySession = useInterpretationEventsStore(
    (s) => s.optedOutBySession,
  );

  // Gate legibility (elspeth-088bf83922 T-2): the version-matched audit
  // snapshot, read the same way AuditReadinessPanel.tsx guards its own
  // snapshot selector — a cached snapshot for a stale composition version
  // must not be used to decide whether to show the advisory note below.
  const compositionVersion = compositionState?.version ?? null;
  const auditSnapshot = useAuditReadinessStore((s) => {
    if (!activeSessionId || compositionVersion === null) return undefined;
    const cached = s.snapshotsBySession[activeSessionId];
    return cached?.composition_version === compositionVersion ? cached : undefined;
  });

  // R2-F7 (elspeth-27bc704359): the same catalog transforms list the
  // audit-readiness panel reads, used below to classify network-egress
  // lines in the pre-run disclosure by `audit_characteristics` rather than
  // the Azure-only hardcoded fallback. `error` is read too: the store
  // resets `transforms` to `null` on BOTH "not loaded yet" and "load
  // failed", and only `error` distinguishes them — collapsing that
  // distinction previously meant a catalog fetch failure silently and
  // permanently fell back to the same under-disclosing hardcoded set R2-F7
  // exists to fix (finding elspeth-27bc704359 follow-up).
  const catalogTransforms = usePluginCatalogStore((s) => s.transforms);
  const catalogSources = usePluginCatalogStore((s) => s.sources);
  const catalogIsLoading = usePluginCatalogStore((s) => s.isLoading);
  const catalogLoadFailed = usePluginCatalogStore((s) => s.error !== null);

  const reactId = useId();
  const describedById = `${reactId}-run-block-reason`;
  const [showRunDisclosure, setShowRunDisclosure] = useState(false);
  const [skipFutureDisclosure, setSkipFutureDisclosure] = useState(false);

  const optedOut = activeSessionId
    ? optedOutInterpretationsBySession[activeSessionId] ?? false
    : false;
  const pendingCount = activeSessionId
    ? Object.keys(pendingInterpretationsBySession[activeSessionId] ?? {}).length
    : 0;
  const isRunBlocked = !optedOut && pendingCount > 0;

  const canExecute =
    activeSessionId !== null &&
    validationResult?.readiness?.execution_ready === true &&
    !isExecuting &&
    progress?.status !== "running" &&
    !isRunBlocked;

  // Gate legibility (elspeth-088bf83922 T-2) — derived, non-gating. These
  // three inputs mirror `canExecute`'s own conditions exactly; none of them
  // feed back into `canExecute`.
  const blockReason = primaryRunBlockReason({
    isExecuting,
    progressRunning: progress?.status === "running",
    isRunBlocked,
    validationNotRun: validationResult == null,
    validationFailing: validationResult != null && validationResult.is_valid !== true,
    executionReadinessBlocked:
      validationResult != null &&
      validationResult.readiness?.execution_ready !== true,
  });
  const blockReasonText =
    blockReason === "readiness"
      ? validationResult?.readiness?.blockers[0]?.detail ??
        RUN_BLOCK_REASON_TEXT.readiness
      : blockReason
        ? RUN_BLOCK_REASON_TEXT[blockReason]
        : null;
  const advisoryRowsNonGreen =
    auditSnapshot?.rows.some(
      (row) =>
        !isRunGatingReadinessRow(
          row.id,
          auditSnapshot.validation_result.readiness.execution_ready,
        ) &&
        (row.status === "warning" || row.status === "error"),
    ) ?? false;

  // Launch, then switch the workspace to the Run artifact
  // (elspeth-3a7b7c7b37): all run-lifecycle feedback (ProgressView's
  // progress bar, Cancel, and its terminal live region) mounts only inside
  // the Run panel, so a successful launch must bring it on screen. Keyed on
  // execute()'s returned run_id being a real id — the null returns (428
  // fanout guard, 409 conflict, interpretation block, stale-session drop)
  // must not switch tabs. Same dispatch idiom as the palette's Export YAML
  // command.
  const launchRun = useCallback(
    async (sessionId: string): Promise<void> => {
      const runId = await execute(sessionId);
      if (typeof runId === "string") {
        dispatchArtifactViewIntent({
          tab: "run",
          focusMode: false,
          sessionId,
        });
      }
    },
    [execute],
  );

  const handleRunClick = useCallback((): void => {
    // Blocked-but-focusable case (aria-disabled): activation is a no-op.
    if (!canExecute || !activeSessionId) return;
    if (disclosureAcknowledged) {
      void launchRun(activeSessionId);
      return;
    }
    setShowRunDisclosure(true);
  }, [activeSessionId, canExecute, disclosureAcknowledged, launchRun]);

  useEffect(() => {
    function handleRunRequest(): void {
      handleRunClick();
    }
    window.addEventListener(REQUEST_RUN_EVENT, handleRunRequest);
    return () => window.removeEventListener(REQUEST_RUN_EVENT, handleRunRequest);
  }, [handleRunClick]);

  function handleDisclosureConfirm(): void {
    if (!canExecute || !activeSessionId) {
      setShowRunDisclosure(false);
      return;
    }
    if (skipFutureDisclosure) {
      acknowledgeRunDisclosure(activeSessionId);
    }
    setShowRunDisclosure(false);
    void launchRun(activeSessionId);
  }

  if (!activeSessionId) return null;

  const egressLines = buildRunEgressSummary(
    compositionState,
    catalogTransforms,
    catalogLoadFailed,
    catalogSources,
    catalogIsLoading,
  );

  return (
    <>
      <Button
        // variant="danger" is a DELIBERATE 2026-08-15 operator decision that
        // supersedes the earlier no-emphasis rule (elspeth-0d37694c8c): Run
        // is the one verb with consequences outside the composer (provider
        // egress and spend), so it carries the danger-family red fill to
        // stand out in both themes. Never "correct" this back to secondary
        // or swap it for variant="primary" — green-as-valid was the exact
        // reading 0d37694c8c removed. The co-equal contract survives as
        // GEOMETRY only (equal widths, workspaceChrome.test.ts). Disabled
        // and aria-disabled states still win by specificity (.btn:disabled
        // is (0,2,0) vs .btn-danger (0,1,0)), so the red reads as "this
        // will actually fire".
        variant="danger"
        className="side-rail-execute-btn"
        onClick={handleRunClick}
        // Native disabled ONLY for reasons without a button-attached
        // explanation. The interpretation-pending block must stay focusable
        // so its aria-describedby reason is reachable (elspeth-94c32de486);
        // shared.css styles .btn[aria-disabled="true"] identically to
        // .btn:disabled, so the visual treatment is unchanged.
        disabled={isRunBlocked ? undefined : !canExecute}
        aria-disabled={!canExecute ? true : undefined}
        aria-label="Run pipeline"
        // `title` (hover tooltip) only when blocked by pending
        // interpretations — the other not-runnable states are natively
        // `disabled`, and a `title` on a disabled element is not reliably
        // reachable by keyboard/AT users, so their reason is carried by the
        // always-visible <p> below instead (elspeth-088bf83922 T-2).
        title={isRunBlocked ? INTERPRETATION_PENDING_RUN_BLOCK_TITLE : undefined}
        aria-describedby={isRunBlocked ? describedById : undefined}
      >
        {isExecuting ? (
          <>
            <span
              className="spinner"
              role="status"
              aria-label="Starting pipeline"
            />
            Starting...
          </>
        ) : (
          "Run pipeline"
        )}
      </Button>
      {/*
        Visually-hidden description for AT users. The `title` attribute on
        the button alone is not reliably announced by all screen readers
        (NVDA reads it; VoiceOver and some JAWS configurations ignore it).
        `aria-describedby` pointing at a hidden span is the WCAG-canonical
        way to surface a "why is this button disabled?" reason — which is
        also why the blocked button must remain focusable (no native
        `disabled`) for the description to be reachable at all.
      */}
      {isRunBlocked && (
        <span id={describedById} className="sr-only">
          {INTERPRETATION_PENDING_RUN_BLOCK_TITLE}
        </span>
      )}
      {/* Gate legibility (elspeth-088bf83922 T-2): a visible (not sr-only)
          one-line reason for whichever gate is currently holding Run back.
          Deliberately plain text, not a tooltip — tooltips on natively
          disabled buttons are not reliably reachable by keyboard/AT users
          (see the WCAG 4.1.2 note above), and this line is meant for every
          user, not just the interpretation-pending case that already has
          its own aria-describedby announcement. */}
      {blockReason && (
        <p
          className="side-rail-execute-reason"
          data-run-block-reason={blockReason}
        >
          {blockReasonText}
        </p>
      )}
      {/* Run is enabled, but the audit-readiness panel has a non-green
          advisory row (plugin trust / provenance / retention / secrets).
          These rows never gate Run — say so in one line rather than
          leaving the user to infer it from an amber/red row that did
          nothing when they ran anyway. */}
      {!blockReason && advisoryRowsNonGreen && (
        <p className="side-rail-execute-reason side-rail-execute-reason--advisory">
          Advisory checks don't block Run.
        </p>
      )}
      {showRunDisclosure && (
        <ConfirmDialog
          title="Run pipeline"
          message={
            egressLines.length > 0
              ? "This run leaves the composer and uses your stored credentials:"
              : "This run leaves the composer and uses your stored credentials."
          }
          confirmLabel="Run pipeline"
          onConfirm={handleDisclosureConfirm}
          onCancel={() => setShowRunDisclosure(false)}
        >
          {egressLines.length > 0 && (
            <ul className="run-disclosure-summary">
              {egressLines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          <label className="run-disclosure-opt-out">
            <Input
              type="checkbox"
              checked={skipFutureDisclosure}
              onChange={(event) => setSkipFutureDisclosure(event.target.checked)}
            />
            <span>Don't ask again for this session</span>
          </label>
        </ConfirmDialog>
      )}
    </>
  );
}
