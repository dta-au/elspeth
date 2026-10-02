# R21. The model-facing disclosure says the review "did not clear" on outage blocks where no review was obtained

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-02-advisor#4 |

## Finding

- **Location:** `src/elspeth/web/composer/service.py:6895-6907` (the unconditional write) and `:11172-11178` (the text).
- **Wrong:** The `_ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE` row is written on every terminal block, including UNAVAILABLE and MALFORMED ones. By the codebase's own definition (`no_tool_policy.py:175-185`), "did not clear" is true only when the advisor gave a verdict. **Scenario:** both advisor passes are unavailable on turn N. On turn N+1 the model may tell the user the reviewer rejected the pipeline.
- **Fix:** Choose the disclosure wording by failure class (not cleared, versus could not be obtained). Keep it as fixed backend copy.
- **Sources:** seam-02-advisor#4.
- **Verifier notes:** This is not new relative to the base. At `7c986dc97` the same text was already written on every block, and 4e0d5d0d9 restored that behaviour. The row's instruction to the model (check the authoritative state) is correct in both failure classes.


## Source findings and verification

### seam-02-advisor#4: Unconditional model-facing disclosure says the review 'did not clear' on outage blocks where no review was obtained

- **Reported at:** `src/elspeth/web/composer/service.py:6906`; reviewer severity low; category stale-copy; diff-anchored True.
- **Summary:** 4e0d5d0d9 made the _ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE row unconditional on every terminal block, including UNAVAILABLE/MALFORMED blocks where nothing was injected. Its text (service.py:11172) opens 'The completion advisory review did not clear'. The codebase's own vocabulary (no_tool_policy.py:175-178) says that phrase is true only when the advisor rendered a verdict.
- **Failure scenario:** Both advisor passes are unavailable on turn N, so the terminal block writes the disclosure. On turn N+1 the model reads that the review 'did not clear' and may tell the user the reviewer rejected the pipeline, when it was never reviewed.
- **Evidence:** service.py:6897-6907 add_message(... advisor_signoff_withheld_control_envelope(_ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE)) with no reason condition; service.py:11172-11178 disclosure text; no_tool_policy.py:175-178 comment on 'did not clear'.
- **Suggested fix:** Choose the disclosure wording by failure class (not cleared versus could not be obtained), keeping it fixed backend copy.
- **Verifier (trace):** upheld, confidence medium, severity low. The finding holds up: an outage-only block does reach the unconditional disclosure write. The loop in the END gate (service.py:6788-6811) re-asks until `verdict.ok` or the budget runs out. After that, `terminal_block` (service.py:6819-6821) is True for `not verdict.ok`. The disclosure is then written at service.py:6895-6907 under nothing but `if session_id is not None`. That happens before `reason` is chosen as 'unavailable'/'malformed' (service.py:6914-6922). The fixed text (service.py:11172-11178) says 'The completion advisory review did not clear'. By the codebase's own definition (no_tool_policy.py:177-185), that is true only when the advisor gave a verdict. That definition is why no_tool_policy.py:186-192 has separate user-facing 'could not be obtained' notices for outages. The model-facing row never got the same split. The recorded ruling (recent-code-hints.md:80-87) requires the row to be written on every block. It says nothing about the wording, so it does not cover this.

One correction to the attribution: 4e0d5d0d9 did not introduce this relative to the window base. At 7c986dc97 the same text was already written unconditionally on every terminal block. A later commit in the window made it depend on `advisor_repair_context_introduced`, f776a8a8d deleted it, and 4e0d5d0d9 put back the base behaviour. So this is an old wording defect on lines the window touched, not a new regression.

Severity stays low. The row's working instruction is correct in both failure classes: don't assume the change was applied, check the authoritative state. The harm is limited to the model possibly describing an outage as a rejection.
