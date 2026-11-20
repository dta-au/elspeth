/**
 * Placeholders the server writes in place of a value it will not show the
 * browser. Each mirrors a backend constant; a test reads the backend source
 * so the two cannot drift.
 */

/** `web/composer/redaction.py` REDACTED_BLOB_SOURCE_PATH: a blob-backed
 *  source's storage path (`path` / `file`), masked whenever it has a `blob_ref`. */
export const REDACTED_BLOB_SOURCE_PATH = "<redacted-blob-source-path>";
