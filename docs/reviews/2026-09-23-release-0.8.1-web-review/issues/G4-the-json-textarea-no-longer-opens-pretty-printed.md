# G4. The JSON textarea no longer opens pretty-printed

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-07#2 |

## Finding

- **Location:** `frontend/src/components/chat/guided/SchemaFormTurn.tsx:388,814`
- **Finding and suggested disposition:** The draft is seeded with `JSON.stringify(v)`, which has no indent. This is cosmetic.
- **Sources:** fe-07#2.

## Source findings and verification

### fe-07#2: JSON textarea no longer opens pretty-printed

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx:388`; reviewer severity low; category ux-regression; diff-anchored True.
- **Summary:** initialValues seeds each JSON field with JsonDraft(JSON.stringify(v)), which has no indentation. jsonText returns the draft's text as-is, so the 2-space pretty branch now runs only when the value is undefined.
- **Failure scenario:** A prefilled schema object such as {"mode":"observed","fields":[...]} opens in the Edit textarea as one long line. Before the window it opened as 2-space-indented JSON. The summary view is unaffected because CodeBlock pretty-prints. This makes the value harder to review and edit but does not block anything.
- **Evidence:** At 7c986dc97, jsonText at lines 781-783 returned JSON.stringify(value ?? emptyForKind(kind), null, 2). At the pinned commit, line 388 is new JsonDraft(JSON.stringify(values[field.name])) and line 814 is if (value instanceof JsonDraft) return value.text.
- **Suggested fix:** Seed the draft with JSON.stringify(values[field.name], null, 2).
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. At the pinned commit, `initialValues` (SchemaFormTurn.tsx:377-391) always wraps each JSON-kind field in `new JsonDraft(JSON.stringify(v))` with no indent argument. It does this for prefilled values, for default values and for `emptyForKind` values. The textarea gets its text from `jsonText` (line 755). `jsonText` returns `value.text` whenever the value is a JsonDraft (line 814), so the 2-space `JSON.stringify(..., null, 2)` branch at line 815 can no longer run for any field that `initialValues` seeded.

No other path undoes this. The two `setValues` paths (lines 86 and 113) only touch `path` and `on_validation_failure` or store what the user typed. Neither re-formats a JSON field.

The base commit pretty-printed these values. At 7c986dc97 the state held parsed objects, and `jsonText` (lines 781-783) returned `JSON.stringify(value ?? emptyForKind(kind), null, 2)` for every value that was not a string.

The change came in 07faf477e ("bind chat cards to authoritative review state"). Its review doc, docs/reviews/2026-09-22-chat-card-repairs.md, says at line 32 only that JSON fields "retain raw editor text independently of parsed values". I found no ruling that the text should start compact, in that doc or in recent-code-hints.md. The regression is cosmetic and nothing is blocked. The file is in the guided lane that is being retired, so low severity stands.
