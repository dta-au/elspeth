# R23. CATEGORY/STEPS parsing does not tolerate markdown emphasis

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-09#4 |

## Finding

- **Location:** `src/elspeth/web/composer/service.py:11037-11038` (the regexes), `:9878-9884` and `:11112-11113`.
- **Wrong:** The machine lines are matched on the raw text. The verdict scan strips emphasis first (`:9859`), and its docstring says live advisors bold their labels. **Scenario:** with `**CATEGORY:** schema_mismatch`, the category falls back to "other", the step ids are dropped, and the literal machine lines leak into the reviewer note. A prose line that starts "Steps:" is taken as the machine line.
- **Fix:** Match after the same emphasis strip, and prefer the last occurrence.
- **Sources:** be-09#4.


## Source findings and verification

### be-09#4: CATEGORY/STEPS parsing does not tolerate markdown emphasis; the header degrades and machine lines leak into the user-visible note

- **Reported at:** `src/elspeth/web/composer/service.py:11037`; reviewer severity low; category correctness; diff-anchored True.
- **Summary:** _ADVISOR_CATEGORY_LINE_RE and _ADVISOR_STEPS_LINE_RE need the label at the start of a raw line. The verdict scan strips emphasis first and documents that live advisors bold their labels. "**CATEGORY:** schema_mismatch" or "- STEPS: x" does not match.
- **Failure scenario:** The advisor ends a FLAG with "**CATEGORY:** schema_mismatch" / "**STEPS:** classify_rows". The category falls back to "other", the step ids are dropped, and both literal lines stay in the reviewer note shown to the user. A prose line starting "Steps:" is taken as the machine line (first match) and deleted from the note.
- **Evidence:** service.py:11037-11038 (regexes), 9878-9884 (parse), 11112-11113 (note .sub). Tests cover only the bare forms (test_advisor_checkpoint.py:1996,2017,2086).
- **Suggested fix:** Match the machine lines after the same emphasis strip the verdict scan uses, and prefer the last occurrence.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. Both regexes (service.py:11037-11038) are anchored with `^\s*LABEL\s*:`. They run on the raw `text` (9878, 9882) and on the note body (11112-11113). Neither sees the emphasis-stripped line that the verdict scan uses (`_ADVISOR_MARKDOWN_EMPHASIS_RE.sub` at 9859). No earlier guard or normaliser handles this. The prompt at 8501-8503 asks for bare lines ("CATEGORY: <...>", "STEPS: <...>"), but nothing enforces that format. The parser's own docstring (9818-9823) says live advisors bold their verdict tokens, so bolded labels are plausible. I found no recorded ruling. The branch review (docs/reviews/2026-09-22-advisor-reviewer-note-final-review.md, M-3 and L8) covers blank lines left behind and emphasis in the verdict lead, not emphasised or bulleted machine labels. The damage is limited and fails safe: the turn is still blocked. The header falls back to the generic "other" sentence and drops the named steps, and the literal machine line shows up in the reviewer note. That makes it low severity.
