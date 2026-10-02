# R25. A NUL character in unadmitted planner prose makes the atomic planner audit cohort unwritable on PostgreSQL

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-07#1 |

## Finding

- **Location:** `src/elspeth/web/composer/pipeline_planner.py:1627-1633`, `:4133`, and `composer/service.py:4867-4874,4884`.
- **Wrong:** `_unadmitted_prose` checks length and UTF-8 but not `\x00`. Before 19f882b8d the cohort held only hashes and metadata. **Scenario:** a nudged PROSE_REPLY contains `\u0000`. On PostgreSQL the whole planner audit write fails, so every call and attempt row is lost and the user gets an `AuditIntegrityError`. Under psycopg2 the error is a raw `ValueError`, which escapes the `except SQLAlchemyError` mapping.
- **Fix:** Return `""` for text containing NUL, as for oversized text, or reject or escape NUL in `WithheldReply.__post_init__`.
- **Sources:** be-07#1.
- **Verifier notes:** Driver behaviour comes from library knowledge and was not executed. Other assistant-content writes may share the gap. They were not checked.


## Source findings and verification

### be-07#1: NUL in unadmitted planner prose makes the all-or-nothing planner audit cohort unwritable on PostgreSQL

- **Reported at:** `src/elspeth/web/composer/pipeline_planner.py:1630`; reviewer severity low; category audit-integrity; diff-anchored True.
- **Summary:** The prose-reply extractor bounds the text by length and checks UTF-8 but does not reject NUL (\x00). The prose is staged and written as raw message content inside the single atomic planner audit write, and PostgreSQL text columns reject NUL.
- **Failure scenario:** The planner returns a text-only reply whose JSON content contains \u0000, and the reply is nudged (PROSE_REPLY). Later the plan succeeds. On PostgreSQL, add_messages_atomic fails on the saved-prose row, so every LLM-call, attempt and invocation row of the request is lost. The user gets AuditIntegrityError 'pipeline planner audit persistence failed before proposal creation'. Under psycopg2 the error is instead a raw ValueError that escapes the except SQLAlchemyError mapping. SQLite deployments do not show the failure.
- **Evidence:** pipeline_planner.py:1627-1633 accepts any bounded str. The staging is at 4133. service.py:4867-4874 writes withheld.content raw as AuditMessageDraft.content in the one add_messages_atomic call, and :4884 maps only SQLAlchemyError. Before 19f882b8d the planner audit write held only hashes, metadata and tool projections, which canonical_json escapes. Driver behaviour was not executed (read-only brief).
- **Suggested fix:** Return "" from _unadmitted_prose for NUL-bearing text, as for oversized text. Alternatively reject or escape NUL in WithheldReply.__post_init__ or withheld_reply_envelope so every withheld-reply site is covered.
- **Verifier (trace):** upheld, confidence medium, severity low. I could not refute it. I traced the code path at 74c0ce0db and nothing on it rejects, strips or escapes NUL. The unadmitted prose goes from the provider response into messages.content inside the single atomic planner-audit write. PostgreSQL text columns reject NUL, so that one row makes the whole cohort fail. The only NUL/control-character guards in web/ cover other data: interpretation values (validation.py:249/277), guided blob refs and CSV cells. None applies to chat or audit message content. Severity stays low because a provider has to actually emit a NUL character in a text-only reply that gets the PROSE_REPLY nudge. The same unguarded-NUL gap probably affects other assistant-content writes too, but I did not check those, so this site may be one instance of a wider class. I also did not run either PostgreSQL driver: the driver behaviour below is from library knowledge, not execution.
