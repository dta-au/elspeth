# G6. A docstring claims `ModifyResponseException` handling matches `_explain_run_diagnostics`

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-20#3 |

## Finding

- **Location:** `web/sessions/_guided_step_chat.py:1023-1028`
- **Finding and suggested disposition:** No such method exists. The public `explain_run_diagnostics` (`composer/service.py:3984-3990`) does not catch that exception. The claim was already false before the window, and the window edited the same line.
- **Sources:** be-20#3.

## Source findings and verification

### be-20#3: Docstring claims ModifyResponseException handling matches _explain_run_diagnostics

- **Reported at:** `src/elspeth/web/sessions/_guided_step_chat.py:1026`; reviewer severity low; category stale-docstring; diff-anchored True.
- **Summary:** The window swapped GuardrailInterventionNormalStringError (absent from litellm 1.102.0) for ModifyResponseException and kept the claim that this matches `_explain_run_diagnostics`. No such method exists; ComposerServiceImpl.explain_run_diagnostics catches only APIError, BudgetExceededError, BlockedPiiEntityError and GuardrailRaisedException.
- **Failure scenario:** A maintainer who trusts the docstring assumes guardrail ModifyResponseException is absorbed on the run-diagnostics path too. It is not: a guardrail intervention there escapes the except clause, while the guided step-chat path absorbs it.
- **Evidence:** _guided_step_chat.py:1023-1028 docstring; composer/service.py:3946 explain_run_diagnostics, except clause at :3984-3990; the installed litellm 1.102.0 has no GuardrailInterventionNormalStringError; ModifyResponseException MRO is (Exception, BaseException).
- **Suggested fix:** Drop or correct the 'matching _explain_run_diagnostics' clause. Make no further investment in the guided lane.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at the pinned commit 74c0ce0db. The docstring at src/elspeth/web/sessions/_guided_step_chat.py:1023-1028 says the absorbed set, now including ModifyResponseException, "mirrors the project canonical in composer/service.py" and matches "_explain_run_diagnostics". Neither claim holds. No `_explain_run_diagnostics` exists anywhere in src/: the only hits are this docstring line, protocol.py:1642 and the public `explain_run_diagnostics` at service.py:3946. That method's except clause at service.py:3984-3990 catches only LiteLLMAPIError, BudgetExceededError, BlockedPiiEntityError and GuardrailRaisedException. ModifyResponseException's MRO is (Exception, BaseException, object), so none of those four catches it. In service.py, neither ModifyResponseException nor GuardrailInterventionNormalStringError appears at the pinned commit or at 7c986dc97. The "matching" claim was therefore already false before the window. Commit 7c986dc97..74c0ce0db edited this exact docstring line (GuardrailInterventionNormalStringError -> ModifyResponseException) and kept the false clause, so the finding is correctly diff-anchored. The impact is documentation only, in a guided file that is being retired, so the severity stays low.
