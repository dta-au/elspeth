// ============================================================================
// preferencesDecoder — structural decoder for the account-level composer
// preferences payload (elspeth-7d07df6438). `parseResponse` (api/client.ts:208)
// ends in an unchecked `as T` cast, so before this decoder an omitted
// `show_advanced` loaded `undefined` into a field preferencesStore declares
// `boolean`.
//
// Be precise about the symptom, because the obvious guess is wrong: React
// renders `open={undefined}` and `open={false}` identically, so no
// `<details open={showAdvanced}>` closed "by accident" — every coercing
// consumer rendered as it does at the intended default. The one
// distinguishable consumer is ComposerPreferencesPanel.tsx:178,
// `checked={showAdvanced}`: `checked={undefined}` makes that radio
// UNCONTROLLED, so it decouples from the store and stops reflecting the
// preference, while its `checked={!showAdvanced}` sibling at :167 stays
// controlled. That is the user-visible defect this decoder closes.
//
// Exact-record discipline for the account preference contract.
// ============================================================================

import type {
  PersistedTutorialStage,
  UserComposerPreferencesPayload,
} from "@/types/api";

// Record keyed by the stage union, rather than a Set built from a literal
// array, so a new stage requires an explicit decoder decision.
//
// The backend carries a lockstep-extension covenant naming the call sites
// that must move together (the module docstring on web/preferences/models.py,
// and the comment above its `TutorialStage` Literal); `MODES` and `STAGES`
// below is the fourth site it names. Referenced by symbol, not by line
// number — the earlier line-range citation went stale the first time that
// docstring grew.
//
// The decoder's KEYS tuple is a SECOND, independent contract with the same
// module: not which VALUES a field admits, but which FIELDS exist at all.
// That one is gated executably by
// tests/unit/web/composer/test_preferences_decoder_parity.py, which compares
// KEYS against `ComposerPreferences.model_fields` — so a backend field
// addition fails pytest rather than throwing `unexpected <key>` on every GET
// in production.
const STAGES: Record<PersistedTutorialStage, true> = {
  build: true,
  run: true,
  audit: true,
  graduation: true,
};
const KEYS = [
  "freeform_intro_dismissed_at",
  "tutorial_completed_at",
  "tutorial_stage",
  "tutorial_session_id",
  "tutorial_run_id",
  "tutorial_source_data_hash",
  "show_advanced",
  "updated_at",
] as const;

function invalid(path: string, detail: string): never {
  throw new Error(`Invalid composer preferences at ${path}: ${detail}`);
}

function exactRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) invalid(path, "expected object");
  const result = value as Record<string, unknown>;
  for (const key of KEYS) {
    if (!Object.prototype.hasOwnProperty.call(result, key)) invalid(path, `missing ${key}`);
  }
  const allowed: ReadonlySet<string> = new Set(KEYS);
  for (const key of Object.keys(result)) {
    if (!allowed.has(key)) invalid(path, `unexpected ${key}`);
  }
  return result;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string") invalid(path, "expected string or null");
  return value;
}

function booleanValue(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") invalid(path, "expected boolean");
  return value;
}

export function decodeUserComposerPreferences(value: unknown): UserComposerPreferencesPayload {
  const path = "composer-preferences";
  const r = exactRecord(value, path);
  const stage = r.tutorial_stage;
  if (stage !== null && (typeof stage !== "string" || !Object.prototype.hasOwnProperty.call(STAGES, stage))) {
    invalid(`${path}.tutorial_stage`, "expected build|run|audit|graduation|null");
  }
  return {
    freeform_intro_dismissed_at: nullableString(r.freeform_intro_dismissed_at, `${path}.freeform_intro_dismissed_at`),
    tutorial_completed_at: nullableString(r.tutorial_completed_at, `${path}.tutorial_completed_at`),
    tutorial_stage: stage as PersistedTutorialStage | null,
    tutorial_session_id: nullableString(r.tutorial_session_id, `${path}.tutorial_session_id`),
    tutorial_run_id: nullableString(r.tutorial_run_id, `${path}.tutorial_run_id`),
    tutorial_source_data_hash: nullableString(r.tutorial_source_data_hash, `${path}.tutorial_source_data_hash`),
    show_advanced: booleanValue(r.show_advanced, `${path}.show_advanced`),
    updated_at: nullableString(r.updated_at, `${path}.updated_at`),
  };
}
