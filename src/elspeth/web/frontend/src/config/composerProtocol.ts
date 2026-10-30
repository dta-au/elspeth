import type { ComposerProtocolVersion } from "@/types/composerOperations";

export const COMPOSER_OPERATION_VERSION_HEADER = "Composer-Operation-Version";

// The Composer-Operation-Version a NEW submission requests, published by the
// app's single /api/system/status poll. Every compose submission (freeform and
// tutorial alike) reads it in client.sendMessage/recompose; no caller passes
// a version. Persisted custody keeps the version the server echoed.
let admissionVersion: ComposerProtocolVersion = 1;

export function composerAdmissionVersion(): ComposerProtocolVersion {
  return admissionVersion;
}
export function setComposerAdmissionVersion(value: unknown): void {
  admissionVersion = value === 2 ? 2 : 1;
}
/** The version a response echoed; a missing echo (an older server) means 1. */
export function echoedComposerVersion(response: Response): ComposerProtocolVersion {
  return response.headers.get(COMPOSER_OPERATION_VERSION_HEADER) === "2" ? 2 : 1;
}
