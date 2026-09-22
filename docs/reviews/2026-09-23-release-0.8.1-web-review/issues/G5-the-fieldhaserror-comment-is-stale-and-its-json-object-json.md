# G5. The `fieldHasError` comment is stale, and its `json-object`/`json-array` string arms are unreachable

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-07#3 |

## Finding

- **Location:** `SchemaFormTurn.tsx:456-479`
- **Finding and suggested disposition:** JSON fields now hold a `JsonDraft` or `undefined`, and a bare `//` line is left over. No runtime effect.
- **Sources:** fe-07#3.

## Source findings and verification

### fe-07#3: fieldHasError comment is stale and its json-object/json-array string arms are unreachable

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx:461`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** The comment above fieldHasError still says the check covers 'a JSON object/array whose text failed to parse' and that 'onChange handlers keep that raw text'. The window also left a dangling '//' line there. JSON fields now hold a JsonDraft (all three kinds, including wrong-container parses) or undefined, never a raw string, so the case json-object/json-array typeof-string arm at lines 477-479 is dead code.
- **Failure scenario:** No runtime failure. A maintainer who trusts the comment would conclude that json-value is still exempt and that raw strings still reach this function, and could edit the dead branch while the real check in the JsonDraft block goes unchanged.
- **Evidence:** Lines 456-461: the comment, with an empty '//' line left by the deleted json-value paragraph. Lines 477-479: return typeof value === "string" && value.trim() !== "". Every JSON write goes through JsonDraft (lines 388 and 761), and the only other state for a JSON field is deletion to undefined (line 117).
- **Suggested fix:** Rewrite the comment to describe JsonDraft's behaviour and delete the unreachable json-object/json-array arms.
- **Verifier (trace):** upheld, confidence high, severity low. I traced it at 74c0ce0db and could not refute it. Every way a JSON-kind field gets a value produces either a JsonDraft or undefined, so the `typeof value === "string"` arm for json-object and json-array can never be reached. Commit 07faf477e deleted the json-value exclusion paragraph and left a bare `//` behind. The comment above it now describes the check incompletely: it covers only object/array parse failure, while the JsonDraft block also blocks json-value parse failures and wrong-container parses. One part of the comment is still roughly true: JsonDraft keeps the raw text in `.text`. No runtime effect, so severity stays low.
