// src/api/blobListing.ts
import type { BlobMetadata } from "@/types/api";

/** The list route's own maximum page size (`limit` is `le=500`). */
export const BLOB_LIST_PAGE_SIZE = 500;

/**
 * Most files `listBlobs` will collect for one session. The list route returns
 * one page per request, so the client pages until the session is exhausted;
 * this bounds that loop. Reaching it is reported, never silent.
 */
export const BLOB_LIST_MAX_FILES = 10_000;

/**
 * The session holds more files than `BLOB_LIST_MAX_FILES`. `blobs` carries
 * the newest files that were collected, so a caller can still show them
 * beside the message instead of an empty list.
 */
export class BlobListTruncatedError extends Error {
  readonly blobs: BlobMetadata[];

  constructor(blobs: BlobMetadata[]) {
    super(
      `This session has more than ${BLOB_LIST_MAX_FILES.toLocaleString("en-US")} files. Only the newest ${BLOB_LIST_MAX_FILES.toLocaleString("en-US")} are listed; older files are not shown.`,
    );
    this.name = "BlobListTruncatedError";
    this.blobs = blobs;
  }
}
