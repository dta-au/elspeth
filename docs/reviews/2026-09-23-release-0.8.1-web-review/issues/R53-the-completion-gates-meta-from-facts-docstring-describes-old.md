# R53. The `completion_gates_meta_from_facts` docstring describes old behaviour

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-17#4 |

## Finding

- **Location:** `src/elspeth/web/execution/completion_gates.py:188`.
- **Wrong:** It says the serializer is used only by save paths with no advisor decision of their own. After 41aeaeac0 it serializes every turn-end save (`_helpers.py:2763`).
- **Fix:** Rewrite it.
- **Sources:** be-17#4.
- **Verifier notes:** The other half of the finding is wrong. The note comment at line 72 correctly refers to the advisor-response parser, which does bound it.


## Source findings and verification

### be-17#4: completion_gates_meta_from_facts docstring and note comment describe old behaviour

- **Reported at:** `src/elspeth/web/execution/completion_gates.py:188`; reviewer severity low; category stale-docstring; diff-anchored True.
- **Summary:** The docstring says the serializer is used only by save paths with no advisor adjudication, but after 41aeaeac0 it serializes every turn-end save, including explicit decisions. The AdvisorSignoffGateDict.note comment (line 72) says the note is bounded at the parser, but parse_completion_gates enforces no bound.
- **Failure scenario:** A maintainer reads the docstring and changes completion_gates_meta_from_facts on the assumption that only recovery persists use it, which breaks turn-end persistence of CLEAN/BLOCKED decisions at _helpers.py:2763. Or they rely on parse_completion_gates to bound note length when it does not.
- **Evidence:** _helpers.py:2763 calls completion_gates_meta_from_facts(resolve_completion_gate_facts(prior_facts, advisor_gate_decision, state)). The ADVISOR_NOTE_MAX_CHARS bound lives only in composer/service.py:11033/11119, and parse_completion_gates at :302-305 checks only for a non-empty str.
- **Suggested fix:** Rewrite both to describe current behaviour.
- **Verifier (trace):** upheld, confidence high, severity low. Half of this finding holds and half does not. The docstring half is correct. Commit 41aeaeac0 deleted `completion_gates_meta_value`, which used to be the serializer for compose saves. That makes `completion_gates_meta_from_facts` the only serializer, and the turn-end writer now uses it for explicit CLEAN/BLOCKED decisions. The docstring (blame 5166baab22, not updated) still says it is used "by save paths that carry NO advisor adjudication of their own (recovery persists…)" and that the fact is "parsed off the prior row first … then re-emitted unchanged". Both statements are now false for the main caller. The note-comment half misreads the code. "Bounded and sanitised at the parser (ADVISOR_NOTE_MAX_CHARS)" refers to the advisor-response parser `_parse_advisor_checkpoint_guidance`, which bounds the note through `_advisor_note_text`. It does not refer to `parse_completion_gates`, and that comment is accurate. Severity stays low because only a docstring is stale and runtime behaviour is correct.
