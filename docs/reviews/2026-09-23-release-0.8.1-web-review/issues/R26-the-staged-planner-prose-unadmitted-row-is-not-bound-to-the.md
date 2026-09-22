# R26. The staged `planner_prose_unadmitted` row is not bound to the call that produced it

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-07#2 |

## Finding

- **Location:** `src/elspeth/web/composer/pipeline_planner.py:4133` and `withheld_replies.py:57-66`.
- **Wrong:** The envelope carries only the origin, schema and content hash. Blank and oversized replies are skipped, and the rows are appended after all call rows. An auditor cannot tell which `planner_call_ordinal` produced the saved words.
- **Fix:** Carry `planner_call_ordinal`, and optionally a truncation flag, on `WithheldReply` for the planner origin.
- **Sources:** be-07#2.


## Source findings and verification

### be-07#2: Staged planner_prose_unadmitted row is not bound to the physical call that produced it

- **Reported at:** `src/elspeth/web/composer/pipeline_planner.py:4133`; reviewer severity low; category audit-integrity; diff-anchored True.
- **Summary:** The saved row holds only origin, schema and content hash. Blank and oversized replies are skipped, the rows are appended after all call and attempt rows, and ComposerLLMCall has no response hash, so the kept text cannot be traced to a planner_call_ordinal or provider_request_id.
- **Failure scenario:** Three planner turns produce prose: an empty reply (attempt prose_nudged, not staged), a reply over 65,536 characters (prose_nudged, not staged), then a truncated prose reply (staged). The audit cohort then holds three prose-class attempts and one withheld row. An auditor cannot tell which call produced the saved words.
- **Evidence:** pipeline_planner.py:4133 stages (origin, content) only, although call.planner_call_ordinal is in scope. withheld_replies.py:57-66 builds the envelope, service.py:4867-4874 appends the rows after the other drafts, and audit.py:272-273 skips blank content.
- **Suggested fix:** Carry planner_call_ordinal, and optionally a truncation flag, on WithheldReply for the planner origin and write it into the envelope.
- **Verifier (trace):** upheld, confidence medium, severity low. I checked every factual claim at 74c0ce0db and all of them hold. Nothing links the staged planner_prose_unadmitted row to the physical call that produced it. Blank and oversized PROSE_REPLY calls leave no row, so matching rows to calls by position in the cohort is unreliable, and an auditor has no way to verify the match. I found no guard, no other binding field and no recorded ruling that says unbound rows are intended. The reply-withholding review asked only that the words be recoverable, and they are: the text is kept in full. That is why the impact stays low. The gap is attribution. No evidence is lost or corrupted, and an auditor can often narrow the match using completion_tokens and error_message. The same unbound envelope is used for every withheld origin, but for the planner origin the ambiguity is sharper, because its rows are appended after all call and attempt rows rather than next to the turn they came from.
