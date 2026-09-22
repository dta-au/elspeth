# R24. Unreachable `None` arm on `_advisor_blocked_result(assistant_message)`

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-09#6 |

## Finding

- **Location:** `src/elspeth/web/composer/service.py:8521` and `:8612`.
- **Wrong:** The parameter was widened to `| None` with a docstring about "a turn that produced no admitted reply". The only production caller (`:6915`) passes a non-optional message. The widening exists so that tests can pass `None`.
- **Fix:** Restore the non-optional type and give the tests a real message.
- **Sources:** be-09#6.
- **Verifier notes:** The `None` tolerance is described in recent-code-hints.md:80-82 and in f776a8a8d. That records what the helper does, not a real producer.


## Source findings and verification

### be-09#6: Unreachable None arm on _advisor_blocked_result(assistant_message)

- **Reported at:** `src/elspeth/web/composer/service.py:8521`; reviewer severity low; category dead-code; diff-anchored True.
- **Summary:** assistant_message was widened to _AdmittedAssistantMessage | None, with an is-not-None guard at 8612 and a docstring describing a turn with no admitted reply. The only caller (6915) always passes a non-None message.
- **Failure scenario:** No runtime failure. The docstring and type describe a path that does not exist, which misleads future editors about which producer shapes are reachable.
- **Evidence:** service.py:8521 and 8612. The single call site is service.py:6915, passing the gate's non-optional assistant_message parameter.
- **Suggested fix:** Restore the non-optional type and drop the None branch and its docstring sentence.
- **Verifier (trace):** upheld, confidence medium, severity low. I confirmed the finding at the pinned commit 74c0ce0db. The only production caller of `_advisor_blocked_result` is `service.py:6915`, inside `_evaluate_terminal_no_tool_advisor_gate` (def at `service.py:6672`). That function declares `assistant_message: _AdmittedAssistantMessage` with no `| None` at `service.py:6678`. It never reassigns the name before passing it through at line 6915. Every other production method that takes `assistant_message` (`service.py:3578`, 5459, 6088, 6418, 6530, 6678) is also non-optional. So in production, the None arm at `service.py:8612` cannot be reached.

The only callers that pass `assistant_message=None` are tests: `test_service.py:2567`, `test_advisor_checkpoint.py:1613` and 1684 (the tolerance test is at 1809), and `test_advisor_terminal_publication.py:334`. Three of those None call sites already existed at the base commit 7c986dc97. They worked then only because the old code set `raw_content = "" if prose_withheld else (...)`. Once f776a8a8d removed the withholding, a None would have raised an AttributeError. The author widened the production signature instead of giving the tests a real message, so a test shortcut leaked into the production type.

Mitigation: the None tolerance is deliberate. `docs/agents/recent-code-hints.md:80-82` and the f776a8a8d commit message both state that "`assistant_message=None` is empty prose under the same notice". But that records what the helper does, not a ruling that a production turn with no admitted reply exists. The docstring at around line 8540 ("may be None for a turn that produced no admitted reply") still describes a producer that does not exist, which can mislead future editors. There is no runtime impact, so the severity stays low.
