# R70. A blob delete failure with an empty `statusText` shows no error

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-11#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/blobStore.ts:200`, `api/client.ts:262,148` and `BlobManager.tsx:158`.
- **Wrong:** `detail ?? "Failed to delete file."` keeps `""`, which is what HTTP/2 sends as `statusText` for a non-JSON 502 or 504. Nothing renders.
- **Fix:** Use `||`, or normalise an empty `statusText` in `parseResponse`.
- **Sources:** fe-11#1.
- **Verifier notes:** This is a regression from 7d981d6cf, which replaced a message that always showed.


## Source findings and verification

### fe-11#1: Delete failure with an empty statusText shows no error

- **Reported at:** `src/elspeth/web/frontend/src/stores/blobStore.ts:200`; reviewer severity low; category error-handling; diff-anchored True.
- **Summary:** `(err as ApiError).detail ?? "Failed to delete file."` keeps an empty-string detail. parseResponse sets detail from response.statusText, which is "" over HTTP/2 when the body is not JSON. BlobManager renders `{error && ...}`, so nothing appears.
- **Failure scenario:** The app is served over HTTP/2 and the DELETE /blobs/{id} returns a 502 or 504 HTML page from ingress. The store sets error "", no message renders, and the file stays listed with no feedback. Before the window the fixed 'Failed to delete file.' always showed.
- **Evidence:** api/client.ts:262 `let detail = response.statusText;` is kept on JSON parse failure (:453-455). api/client.ts:148 notes statusText is "" over HTTP/2. BlobManager.tsx:158 `{error && (`.
- **Suggested fix:** Use `||` instead of `??`, or normalise an empty statusText to undefined in parseResponse.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. I traced the path at 74c0ce0db and it is reachable. Nothing between `fetch` and the store replaces an empty `statusText`. The `??` fallback in blobStore does not catch `""`. BlobManager hides the error box when the error string is empty. This is a regression from the window's commit 7d981d6cf, which replaced a message that always showed. Severity stays low. It needs a non-JSON or detail-less error body, and a transport that sends an empty status phrase, which is always the case over HTTP/2. The delete fails safely: the file stays in the list, and only the feedback is missing.
