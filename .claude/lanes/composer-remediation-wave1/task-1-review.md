# Q1 independent scoped review

**Spec compliance: PASS. Code quality: APPROVED.** No critical, important, or minor issues identified in the supplied patch.

Reviewed the task brief, implementation report, and `task-1-review.diff` once against base `6825994b482bc274df2ae85d8738b9078f0117b4`. Scope is the two added production lines and the new 187-line HTTP regression module. No code changes or test reruns were performed.

## Compliance and behavior evidence

- `src/elspeth/web/sessions/routes/composer/proposals.py:506` constructs the runtime policy from `request.app.state.settings.secret_wiring_allowlist` and supplies it to the existing real `execute_tool` call. Neither proposal arguments nor model output contribute authorization. The existing converter preserves all four exact-match fields and the empty policy remains deny-by-default.
- `tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py:127` exercises HTTP acceptance with a supported `azure_blob` / `sas_token` destination. It asserts 200, committed response and stored proposal status, a new state ID and incremented version, the persisted replacement marker, a valid runtime-preflight result, and the committed state identity on the proposal.
- `tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py:149` retries the committed proposal and asserts the existing terminal 409 behavior, the same persisted state, and exactly two state versions. This pins the legacy acceptance contract; it does not claim receipt-backed pipeline replay was newly tested.
- `tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py:155` varies absent policy and each authorization axis independently: secret, component type, plugin, and option key. Every denial asserts the policy-specific 422 envelope, rejected stored proposal status, no committed-state identity, exact prior-state preservation, and unchanged version history. These are behavioral assertions, not mock-argument checks.
- Targeted inspection of the reused `_make_app`, state/proposal factories, route dispatch/settlement context, and policy converter addressed the concrete risks of mocked authorization/preflight, unfenced fixture persistence, and altered policy semantics. The fixture retains the real dispatch and preflight path, uses existing fenced persistence helpers, and only replaces the backing secret store with a specced double while retaining the scoped resolver. The previous marker makes the mutation observable; the tests explicitly avoid claiming the seeded graph is execution-ready under mismatching deployment policy.

The production patch changes only the missing policy input. Provider attribution, argument validation, operation authority and lease context, required-control finalization, preflight, and proposal settlement remain on the existing path. No execute acknowledgement, custody/review mechanism, D1 staging behavior, tool/settings/persistence schema, or public error envelope is changed.

## Findings

- Critical: none.
- Important: none.
- Minor: none.

## Verification limits

The implementation report records the pre-fix red control (authorized case refused while five denied cases passed), subsequent six-case green run, seven existing acceptance cases, and successful contracts, scoped mypy, and Ruff checks. These are reported implementation evidence, not independently rerun results. The parent-owned 571-case gate was pending when this review was commissioned; this approval does not assert its success or authorize treating an unfinished run as green. The parent must inspect its terminal result and the remaining integration gates before integration.
