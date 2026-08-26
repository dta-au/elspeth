// ============================================================================
// validationHumaniser — plain-language rendering of backend validation dumps.
//
// Relocated out of components/chat/guided/PipelineValidationSummary.tsx
// (elspeth-d9e5d157cb) so NON-component layers can share it. The chat
// validation-failure injection lives in a store (stores/subscriptions.ts) and
// could not reach the humaniser while it lived in the component tree, so it
// leaked raw engine dumps + a "[type] id:" internal-id prefix into chat. This
// pure module (no React, no store) is importable by the store, the audit panel
// (ReadinessRowDetail), and the chat components alike — so all four
// novice-register surfaces name steps and phrase failures identically.
//
// Depends only on pure leaves (pipelineGloss, interpretationStepLabel,
// catalog/pluginDisplayName) + types/utils — none of them import React, a
// store, or this module, so there is no import cycle.
// ============================================================================

import {
  buildPlainPhraseMap,
  COALESCE_PHRASE,
  PROCESS_ROW_PHRASE,
  QUEUE_PHRASE,
  READ_API_PHRASE,
  READ_CSV_PHRASE,
  READ_DATA_PHRASE,
  READ_JSON_PHRASE,
  SCRAPE_PAGE_PHRASE,
  ROW_UNION_PHRASE,
  UNKNOWN_COMPONENT_PHRASE,
  WRITE_CSV_PHRASE,
  WRITE_JSON_PHRASE,
  WRITE_RESULTS_PHRASE,
} from "@/components/chat/guided/pipelineGloss";
import {
  descriptionLabel,
  isPluginDerivedId,
} from "@/components/chat/interpretationStepLabel";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import type { CompositionState, NodeSpec } from "@/types/index";
import {
  sortedSourceEntries,
  sourceComponentId,
} from "@/utils/compositionState";

/**
 * Match the engineer-grade contract-violation dumps that reach the validation
 * surfaces. Three backend producers write them (all egress verbatim via the
 * validation endpoint's `message=str(exc)`):
 *   - composer authoring (web/composer/state.py):
 *     "Schema contract violation: 'producer' -> 'consumer'. …"
 *     "Transform contract violation: node 'producer' (plugin). …"
 *     "Transform output guarantee violation: node 'producer' (plugin). …"
 *   - DAG runtime preflight (core/dag/graph.py) — the live-verified format:
 *     "Schema contract violation: edge 'producer' → 'consumer'\n  Consumer …"
 *   - edge-contract preflight (web/execution/validation.py):
 *     "Edge contract violation between producer node 'X' (schema 'S') and
 *      consumer node 'Y' (schema 'T'):\n…"
 * Capture groups: [1] = producer id, [2] = consumer id (optional).
 */
const CONTRACT_VIOLATION_RES: readonly RegExp[] = [
  /^(?:Schema|Semantic|Transform) contract violation: (?:(?:edge|node) )?'([^']+)'(?: (?:->|→) '([^']+)')?/,
  // Rule C's own headline since elspeth-920bd88299: it was split off
  // "Transform contract violation" so the two rules stop sharing one
  // error_code, and with it one set of repair advice.
  /^Transform output guarantee violation: node '([^']+)'/,
  /^Edge contract violation between producer node '([^']+)' \(schema '[^']*'\) and consumer node '([^']+)'/,
];

/**
 * Match the interpretation-review-pending dumps that reach this surface
 * verbatim from `_format_interpretation_site` (web/execution/validation.py):
 *   "pipeline_decision review pending for transform 'guided_xform_1': drop_raw_html_fields"
 * Capture group: [1] = component id. The kind / component_type / user_term are
 * engineer detail — they stay in the raw dump behind the expander
 * (elspeth-016f463ff0).
 */
const INTERPRETATION_REVIEW_PENDING_RE =
  /^[a-z_]+ review pending for [a-z_]+ '([^']+)': /;

export interface HumanisedFinding {
  /** Plain-language headline safe for a role="status" announcement. */
  headline: string;
  /** The verbatim engineer-grade text, kept behind a details expander.
   *  null when the original message is already headline-register. */
  raw: string | null;
  /**
   * How the headline handled step naming (elspeth-9f21f3c57d), consumed by
   * `stepPrefixPhrase` to decide whether a possessive step prefix would add
   * a name or duplicate one:
   *   - "self": the headline template names (or generically references) the
   *     step itself, in the step-label vocabulary — never prefix it.
   *   - array: the RESOLVED phrases the headline embedded (generic
   *     "this step" fallbacks excluded); a component whose phrase is absent
   *     from the list still benefits from a prefix.
   */
  namedSteps: "self" | readonly string[];
}

/**
 * Humanise one validation message. Contract-violation dumps become a
 * plain-language headline ("Two steps aren't connected correctly: …") with the
 * raw dump preserved for the expander; interpretation-review-pending dumps
 * become "The <step> step is waiting for your review." (elspeth-016f463ff0)
 * using the SAME step-label mapping the acknowledgement cards render, when the
 * caller supplies `stepLabelFor`; anything else passes through untouched.
 * Exported for tests.
 */
export function humaniseValidationMessage(
  message: string,
  phraseFor: (componentId: string | null) => string,
  stepLabelFor?: (componentId: string) => string | null,
): HumanisedFinding {
  const pendingMatch = INTERPRETATION_REVIEW_PENDING_RE.exec(message);
  if (pendingMatch !== null && stepLabelFor !== undefined) {
    const stepLabel = stepLabelFor(pendingMatch[1]);
    return {
      // A null step label (component absent from the composition) falls back
      // to a generic phrase — never the raw internal id.
      headline:
        stepLabel !== null
          ? `The ${stepLabel} step is waiting for your review.`
          : "A step is waiting for your review.",
      raw: message,
      namedSteps: "self",
    };
  }
  let match: RegExpExecArray | null = null;
  for (const pattern of CONTRACT_VIOLATION_RES) {
    match = pattern.exec(message);
    if (match !== null) break;
  }
  if (match === null) {
    return { headline: message, raw: null, namedSteps: [] };
  }
  const producerPhrase = phraseFor(match[1] ?? null);
  const consumerPhrase = match[2] !== undefined ? phraseFor(match[2]) : null;
  const headline =
    consumerPhrase !== null
      ? `Two steps aren't connected correctly: the "${producerPhrase}" step's output doesn't match what "${consumerPhrase}" expects.`
      : `A step isn't connected correctly: "${producerPhrase}" doesn't match what the next step expects.`;
  const namedSteps = [producerPhrase, consumerPhrase].filter(
    (phrase): phrase is string =>
      phrase !== null && phrase !== UNKNOWN_COMPONENT_PHRASE,
  );
  return { headline, raw: message, namedSteps };
}

/**
 * Component-id → plain-phrase resolver over the pipeline gloss. Contract dumps
 * prefix node ids ("node:rater") while the gloss map keys on the bare id, so
 * the resolver tries verbatim first, then the stripped form. Shared by the
 * summary headline, the wire-stage blockers list (ChatPanel), the audit panel
 * and the chat injection so every surface names steps identically.
 *
 * `componentType` is an OPTIONAL hint — the structured `component_type` a
 * Stage-2 ValidationError/ValidationWarning carries alongside its
 * `component_id` (types/index.ts). It is consulted only as the LAST resort,
 * after an exact/stripped/fuzzy match against the live composition has
 * already failed (elspeth-ede84df6b3): a real, currently-wired component's
 * phrase always wins over a guess derived from the id text or the type hint.
 */
export function makePhraseFor(
  compositionState: CompositionState | null | undefined,
): (componentId: string | null, componentType?: string | null) => string {
  const phraseMap = buildPlainPhraseMap(compositionState);
  // Overlay each component's OWN identity over the category gloss
  // (elspeth-9f21f3c57d): the ladder is authored description →
  // titleCase(node.id) for an author-meaningful id → the plugin/structural
  // gloss already in the map → UNKNOWN_COMPONENT_PHRASE. This lives here, on
  // the validation-prose surface, NOT in pipelineGloss's sentence
  // composition, whose consecutive-run collapsing needs category phrases.
  if (compositionState !== null && compositionState !== undefined) {
    for (const [name, source] of sortedSourceEntries(compositionState)) {
      const described = descriptionLabel(source.description);
      if (described !== null) phraseMap.set(sourceComponentId(name), described);
    }
    for (const node of compositionState.nodes) {
      const identity = nodeIdentityPhrase(node);
      if (identity !== null) phraseMap.set(node.id, identity);
    }
    for (const output of compositionState.outputs) {
      const described = descriptionLabel(output.description);
      if (described !== null) phraseMap.set(output.name, described);
    }
  }
  // Precomputed once per makePhraseFor() call (not per phraseFor()
  // invocation / per unresolved lookup) — elspeth-40d6efac2b: a validation
  // result can carry many findings, each triggering a fuzzy lookup over the
  // SAME phraseMap, so re-tokenising every known id on every lookup was
  // wasted, repeated work.
  const fuzzyIndex = buildFuzzyIndex(phraseMap);
  return (componentId: string | null, componentType?: string | null): string => {
    if (componentId === null) return UNKNOWN_COMPONENT_PHRASE;
    const direct = phraseMap.get(componentId);
    if (direct !== undefined) return direct;
    const stripped = componentId.replace(/^(node|source|output):/, "");
    const strippedPhrase = phraseMap.get(stripped);
    if (strippedPhrase !== undefined) return strippedPhrase;
    // Defensive compiled-id strip (elspeth-9f21f3c57d) — a FALLBACK only,
    // after the exact/map paths above, so a composer node literally named
    // like a compiled id still wins via its direct entry. Recover the
    // embedded composer id and re-run the map/fuzzy resolution on it.
    const compiledMatch = COMPILED_DAG_ID_RE.exec(stripped);
    if (compiledMatch !== null) {
      const composerId = compiledMatch[1];
      const composerDirect = phraseMap.get(composerId);
      if (composerDirect !== undefined) return composerDirect;
      const composerFuzzy = bestFuzzyMatch(composerId, fuzzyIndex);
      if (composerFuzzy !== null) return composerFuzzy;
    }
    // Try the fuzzy known-component match BEFORE the generic role/format
    // guess (elspeth-66f50ba810): a specific phrase for a real, currently
    // -wired component must not be shadowed by a generic guess just because
    // the unresolved id happens to contain a role token too.
    const fuzzyPhrase = bestFuzzyMatch(stripped, fuzzyIndex);
    if (fuzzyPhrase !== null) return fuzzyPhrase;
    const generatedPhrase = fallbackPhraseForGeneratedId(stripped, componentType);
    if (generatedPhrase !== null) return generatedPhrase;
    return UNKNOWN_COMPONENT_PHRASE;
  };
}

/**
 * Compiled-DAG-id anatomy: `<kind prefix>_<composer node id>_<12-hex hash>`.
 * The prefix vocabulary is the COMPILED node kinds (`config_gate` is the
 * compiled prefix for gate nodes — not the composer node_type "gate"), and
 * the composer id is captured GREEDILY because composer ids may themselves
 * contain underscores: a node named `step_0123456789ab` must resolve.
 */
const COMPILED_DAG_ID_RE =
  /^(?:source|sink|queue|transform|aggregation|config_gate|coalesce|row_union|collector)_(.+)_[0-9a-f]{12}$/;

/**
 * A node's own identity for validation prose (elspeth-9f21f3c57d): its
 * authored description when present, else its author-chosen id title-cased
 * via the ONE acronym-aware title-caser. Null when the node has neither (an
 * id trivially derived from its plugin — or, for structural plugin-less
 * nodes, its node_type — carries no author intent), so the category gloss
 * in the phrase map stands.
 */
function nodeIdentityPhrase(node: NodeSpec): string | null {
  const described = descriptionLabel(node.description);
  if (described !== null) return described;
  // ?? null: defensive against under-specified node objects (test fixtures
  // cast partial nodes into the store) — with nothing to derive from, the id
  // is all author intent there is.
  const derivedFrom = node.plugin ?? node.node_type ?? null;
  if (derivedFrom !== null && isPluginDerivedId(node.id, derivedFrom)) return null;
  return titleCaseLabel(node.id);
}

const MIN_FUZZY_COMPONENT_TOKEN_LENGTH = 4;

type GeneratedRole = "source" | "output" | "transform";

// Single source of truth for the role vocabulary (elspeth-20bb1c3ac4):
// firstGeneratedRole() used to re-declare these same words as a parallel
// regex alternation. COMPONENT_ROLE_TOKENS (which also filters role words
// out of fuzzy-match "meaningful" tokens, below) is derived from this map's
// keys rather than hand-duplicated.
const ROLE_TOKEN_ROLE: Readonly<Record<string, GeneratedRole>> = {
  source: "source",
  input: "source",
  read: "source",
  sink: "output",
  output: "output",
  write: "output",
  transform: "transform",
  xform: "transform",
};

const COMPONENT_ROLE_TOKENS = new Set([...Object.keys(ROLE_TOKEN_ROLE), "node"]);

// Content words that imply a "transform" role even without an explicit role
// token — matched per-token (anchored) when scanning an id for its role.
const TRANSFORM_CONTENT_TOKEN_RE = /^(transform|xform|llm|rate|score|classif.*|summari[sz]e.*)$/;
// Same content-word family, matched as a substring against the whole id —
// used only once a role could not be determined any other way (see
// fallbackPhraseForGeneratedId below).
const TRANSFORM_CONTENT_SUBSTRING_RE = /(llm|rate|score|classif|summari[sz]e|xform|transform)/;

const CSV_RE = /csv/;
const JSON_RE = /json/;
// The source and transform/roleless web-format ladders were never identical
// in the original code (source's included "api", transform's included
// "fetch") — kept distinct here rather than force-unified, which would
// silently change which generated ids each role treats as "web" format.
const SOURCE_WEB_RE = /(api|http|url|web|scrape)/;
const TRANSFORM_WEB_RE = /(scrape|fetch|web|http|url)/;

type ComponentFormat = "csv" | "json" | "web" | null;

function detectFormat(id: string, webRe: RegExp): ComponentFormat {
  if (CSV_RE.test(id)) return "csv";
  if (JSON_RE.test(id)) return "json";
  if (webRe.test(id)) return "web";
  return null;
}

interface RolePhraseEntry {
  webRe: RegExp;
  default: string;
  csv?: string;
  json?: string;
  web?: string;
}

// The {role x format -> phrase} table (elspeth-20bb1c3ac4) replacing the
// three separate role-branch ladders (source/output/transform) that used to
// repeat the same csv/json/web checks with independently-drifting copies.
const ROLE_PHRASE_TABLE: Readonly<Record<GeneratedRole, RolePhraseEntry>> = {
  source: {
    webRe: SOURCE_WEB_RE,
    default: READ_DATA_PHRASE,
    csv: READ_CSV_PHRASE,
    json: READ_JSON_PHRASE,
    web: READ_API_PHRASE,
  },
  output: {
    webRe: TRANSFORM_WEB_RE,
    default: WRITE_RESULTS_PHRASE,
    csv: WRITE_CSV_PHRASE,
    json: WRITE_JSON_PHRASE,
  },
  transform: {
    webRe: TRANSFORM_WEB_RE,
    default: PROCESS_ROW_PHRASE,
    web: SCRAPE_PAGE_PHRASE,
  },
};

function phraseForRoleFormat(role: GeneratedRole, id: string): string {
  const entry = ROLE_PHRASE_TABLE[role];
  const format = detectFormat(id, entry.webRe);
  const specific = format === "csv" ? entry.csv : format === "json" ? entry.json : format === "web" ? entry.web : undefined;
  return specific ?? entry.default;
}

/** Maps a Stage-2 ValidationError/Warning's structured `component_type`
 *  (e.g. "source", "sink", "output", "transform", "node", "graph", …) onto
 *  the same three-way role vocabulary the id-substring guess uses. Values
 *  outside this vocabulary (e.g. "graph", "pipeline") carry no directional
 *  signal and are treated as unknown rather than guessed at. */
function roleFromComponentType(componentType: string | null | undefined): GeneratedRole | null {
  if (componentType === null || componentType === undefined) return null;
  const t = componentType.toLowerCase();
  if (t === "source") return "source";
  if (t === "sink" || t === "output") return "output";
  if (
    t === "transform"
    || t === "node"
    || t === "gate"
    || t === "aggregation"
    || t === "coalesce"
    || t === "row_union"
    || t === "queue"
  ) {
    return "transform";
  }
  return null;
}

function structuralPhraseFromComponentType(
  componentType: string | null | undefined,
): string | null {
  const type = componentType?.toLowerCase();
  if (type === "row_union") return ROW_UNION_PHRASE;
  if (type === "coalesce") return COALESCE_PHRASE;
  if (type === "queue") return QUEUE_PHRASE;
  return null;
}

function componentIdTokens(componentId: string): string[] {
  return componentId.toLowerCase().split(/[_:-]+/).filter(Boolean);
}

function fallbackPhraseForGeneratedId(
  componentId: string,
  componentType?: string | null,
): string | null {
  const id = componentId.toLowerCase();
  // Structural component types are semantic identities, not directional
  // hints. They remain authoritative even when a generated id happens to
  // contain a source/output/transform token.
  const structuralPhrase = structuralPhraseFromComponentType(componentType);
  if (structuralPhrase !== null) return structuralPhrase;
  // For non-structural types, the id's own role token takes precedence over
  // the structured directional hint. The hint fills the gap only when the id
  // carries no role token of its own (elspeth-ede84df6b3).
  const explicitRole = firstGeneratedRole(id);
  if (explicitRole !== null) return phraseForRoleFormat(explicitRole, id);
  const hintedRole = roleFromComponentType(componentType);
  if (hintedRole !== null) return phraseForRoleFormat(hintedRole, id);
  // No role signal at all (neither an id token nor a usable component_type
  // hint). CSV/JSON carry no direction of their own — guessing write-vs-read
  // here previously defaulted to a write-direction phrase and pointed at the
  // wrong end of the pipeline for role-less SOURCE ids (elspeth-ede84df6b3).
  // Only resolve the role-neutral cases; let the caller fall back to the
  // neutral UNKNOWN_COMPONENT_PHRASE for csv/json.
  if (TRANSFORM_WEB_RE.test(id)) return SCRAPE_PAGE_PHRASE;
  if (TRANSFORM_CONTENT_SUBSTRING_RE.test(id)) return PROCESS_ROW_PHRASE;
  return null;
}

function firstGeneratedRole(id: string): GeneratedRole | null {
  const tokens = componentIdTokens(id);
  for (const token of tokens) {
    const mapped = ROLE_TOKEN_ROLE[token];
    if (mapped !== undefined) return mapped;
    if (TRANSFORM_CONTENT_TOKEN_RE.test(token)) return "transform";
  }
  return null;
}

// ── Fuzzy known-component matching (elspeth-8f89b0ba34 / elspeth-40d6efac2b) ─
//
// An unresolved id (e.g. a backend-generated id for a component that no
// longer exists, or one the composer hasn't wired into the gloss map yet)
// can still share meaningful tokens with a REAL known component. Score each
// candidate by how many of the known id's meaningful (>=4-char, non-role)
// tokens appear in the unresolved id, so a specific match ("refunds_clean",
// 2 tokens) beats a coincidental partial one ("refunds_raw", 1 token —
// "raw" is dropped for being under the length floor) regardless of which
// entry the phrase map happens to iterate first.

interface FuzzyIndexEntry {
  phrase: string;
  meaningfulTokens: readonly string[];
  totalTokens: number;
}

function buildFuzzyIndex(phraseMap: ReadonlyMap<string, string>): FuzzyIndexEntry[] {
  const index: FuzzyIndexEntry[] = [];
  for (const [knownId, phrase] of phraseMap.entries()) {
    const allTokens = componentIdTokens(knownId);
    const meaningfulTokens = allTokens.filter(
      (token) => token.length >= MIN_FUZZY_COMPONENT_TOKEN_LENGTH && !COMPONENT_ROLE_TOKENS.has(token),
    );
    if (meaningfulTokens.length === 0) continue;
    index.push({ phrase, meaningfulTokens, totalTokens: allTokens.length });
  }
  return index;
}

function bestFuzzyMatch(componentId: string, index: readonly FuzzyIndexEntry[]): string | null {
  const componentTokens = new Set(componentIdTokens(componentId));
  if (componentTokens.size === 0) return null;
  let best: { score: number; totalTokens: number; phrase: string } | null = null;
  for (const entry of index) {
    if (!entry.meaningfulTokens.every((token) => componentTokens.has(token))) continue;
    const score = entry.meaningfulTokens.length;
    if (
      best === null ||
      score > best.score ||
      (score === best.score && entry.totalTokens > best.totalTokens)
    ) {
      best = { score, totalTokens: entry.totalTokens, phrase: entry.phrase };
    }
  }
  return best?.phrase ?? null;
}

/**
 * The possessive step phrase to prefix a finding with, or null when a prefix
 * would add nothing. Shared by `formatFindingBody` (the status line) and the
 * chat injection's bullet (stores/subscriptions.ts) so the two surfaces
 * cannot drift.
 *
 * A settings-level finding (componentId === null) owns no step — never
 * prefix (elspeth-901a404926). An un-humanised finding (raw === null) keeps
 * the legacy rule: any attributed finding gets its phrase. A humanised
 * finding gets a prefix only when it ADDS a name the headline lacks
 * (elspeth-9f21f3c57d — a resolvable component_id must surface even when the
 * message's own ids were unmappable): never for a "self"-naming headline
 * (review-pending), never the generic UNKNOWN_COMPONENT_PHRASE, and never a
 * phrase the headline already embeds.
 */
export function stepPrefixPhrase(
  finding: HumanisedFinding,
  componentId: string | null,
  componentType: string | null,
  phraseFor: (componentId: string | null, componentType?: string | null) => string,
): string | null {
  if (componentId === null) return null;
  const phrase = phraseFor(componentId, componentType);
  if (finding.raw === null) return phrase;
  if (finding.namedSteps === "self") return null;
  if (phrase === UNKNOWN_COMPONENT_PHRASE) return null;
  if (finding.namedSteps.includes(phrase)) return null;
  return phrase;
}

/**
 * Compose the status-line body: "<N> <label> — <headline>", prefixing the
 * finding's plain-language step name ("'rate each row': …") when
 * `stepPrefixPhrase` above decides the prefix adds a name the headline
 * lacks.
 *
 * `componentType` is the SAME finding's structured `component_type`
 * (ValidationError/ValidationWarning) — required (not optional) so a caller
 * that has the field on hand cannot forget to thread it through to
 * `phraseFor` (elspeth-ede84df6b3). Pass `null` explicitly when the caller
 * genuinely has no structured type for this id (e.g. Stage-1 string-only
 * errors with no component attribution at all).
 */
export function formatFindingBody(
  count: number,
  label: string,
  finding: HumanisedFinding,
  componentId: string | null,
  componentType: string | null,
  phraseFor: (componentId: string | null, componentType?: string | null) => string,
): string {
  const prefix = stepPrefixPhrase(finding, componentId, componentType, phraseFor);
  return prefix !== null
    ? `${count} ${label} — '${prefix}': ${finding.headline}`
    : `${count} ${label} — ${finding.headline}`;
}

/** Closed status `_guided_persisted_validity` stamps on every guided
 *  pre-commit checkpoint: under deferred guided commit the composition
 *  state stays empty until Confirm wiring commits the accepted proposal,
 *  so this status describes the placeholder that the confirm action itself
 *  replaces — never a defect the user can act on. Gating the wire-stage
 *  Confirm on it deadlocks guided authoring (elspeth-859e2702dd); the wire
 *  turn's own server-computed `can_confirm`/`blockers` — validated against
 *  the proposal candidate — remain the authoritative confirm gate. */
export const GUIDED_DEFERRED_COMMIT_STATUS = "guided_composition_invalid";

/** Persisted composition errors that should gate the wire-stage Confirm
 *  client-side (elspeth-3b35abf148 variant 3), with the guided
 *  deferred-commit placeholder excluded. */
export function clientWireBlockerMessages(messages: readonly string[]): string[] {
  return messages.filter((message) => message !== GUIDED_DEFERRED_COMMIT_STATUS);
}
