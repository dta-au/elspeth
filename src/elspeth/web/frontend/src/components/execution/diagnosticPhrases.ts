// ============================================================================
// diagnosticPhrases — reader-register phrases for the diagnostic enums the
// curated failure row renders (elspeth-d74ab492dd). Known values read as
// prose with the raw enum in `title`; anything not listed here stays in
// <code> — an unknown identifier must never be dressed up as a sentence.
// Maps, not objects, so a value named "constructor" cannot collide with
// Object.prototype. Add an entry only for a value ELSPETH itself emits
// (grep the backend for the literal before adding).
//
// These maps are OPEN, and that is the honest choice here — unlike
// POLICY_PHRASES, which closes against a union. types/index.ts declares no
// union for run-failure reason or cause; RunsHistoryDrawer.tsx reads them
// through safeDiagnosticIdentifier, i.e. they are free-form wire strings. With
// no member set to close against, a closed Record would mean inventing the set
// here, which is the thing this wave is correcting. The unknown arm is not a
// silent degradation either: it renders <code>, which is the correct register
// for an identifier nobody has phrased.
//
// The repo's precedent for a phrase map that genuinely CLOSES is
// components/execution/runTerminalPhrases.ts,
// `const TERMINAL_RUN_PHRASES: Record<TerminalRunStatus, TerminalRunPhrase>`,
// whose docblock states the property: adding a status "fails to compile here
// until its phrasing is decided." Use that shape when a union exists.
//
// Axis note (no duplication): runTerminalPhrases.ts phrases run TERMINAL
// STATUSES, and InlineRunResults.tsx phrases DISCARD stages and causes. This
// module phrases run-FAILURE reason/cause. Three axes, three homes; check
// here first before adding a fourth.
// ============================================================================

export const DIAGNOSTIC_REASON_PHRASES: ReadonlyMap<string, string> = new Map([
  ["submit_failed", "the request could not be submitted"],
]);

export const DIAGNOSTIC_CAUSE_PHRASES: ReadonlyMap<string, string> = new Map([
  ["s3_object_unreadable", "the S3 object could not be read"],
  ["provider_rejected", "the provider rejected the request"],
]);
