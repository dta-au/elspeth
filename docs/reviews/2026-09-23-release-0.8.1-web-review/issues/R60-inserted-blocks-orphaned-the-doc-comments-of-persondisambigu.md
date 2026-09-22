# R60. Inserted blocks orphaned the doc comments of `personDisambiguator` and `deleteAdminUser`

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-02#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/api/people.ts:97-112` and `client.ts:577-586`.
- **Wrong:** In 793d0f95a, new constants were inserted between these functions and their JSDoc, so each doc comment now attaches to the wrong symbol.
- **Fix:** Move the comments back.
- **Sources:** fe-02#2.
- **Verifier notes:** `deleteAdminUser` got a new docblock of its own. `personDisambiguator` now has no documentation.


## Source findings and verification

### fe-02#2: Inserted blocks orphaned the doc comments of personDisambiguator and deleteAdminUser

- **Reported at:** `src/elspeth/web/frontend/src/api/people.ts:97`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** 793d0f95a inserted new code between two functions and their JSDoc. PROVIDER_LABEL (people.ts:98-110) now sits between personDisambiguator's doc (people.ts:97) and the function, and DELETE_ADMIN_USER_REASON_MAX_LENGTH (client.ts:578-579) now sits between deleteAdminUser's old doc (client.ts:577) and the function. Each comment now documents the wrong symbol.
- **Failure scenario:** A maintainer hovering over PROVIDER_LABEL sees two stacked docblocks, the first describing the disambiguator line. Hovering over DELETE_ADMIN_USER_REASON_MAX_LENGTH shows 'Delete a local-auth account. Backend returns 204 No Content.' personDisambiguator itself has no documentation.
- **Evidence:** people.ts:97 `/** The line that tells two people with one name apart: never the name again. */` is followed directly by PROVIDER_LABEL's own docblock at 98-102. client.ts:577 `/** Delete a local-auth account. Backend returns 204 No Content. */` is followed by the constant's docblock at 578.
- **Suggested fix:** Move each orphaned comment back directly above its function (personDisambiguator at people.ts:112, deleteAdminUser at client.ts:586), or merge it into the new docblock.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed this at the pinned commit 74c0ce0db, and the window introduced it. In people.ts, commit 793d0f95a put the PROVIDER_LABEL block between personDisambiguator's one-line doc and the function. That doc comment now sits on top of PROVIDER_LABEL's own docblock, and personDisambiguator is left with no documentation. In client.ts, the same commit put DELETE_ADMIN_USER_REASON_MAX_LENGTH directly under the old deleteAdminUser comment. This half is milder than the finding says. deleteAdminUser got a new, correct docblock of its own, so the old line is an orphaned duplicate that now sits on the constant. It does not leave the function undocumented. This is a doc-comment problem only: no runtime behaviour is affected, so the severity stays low.
