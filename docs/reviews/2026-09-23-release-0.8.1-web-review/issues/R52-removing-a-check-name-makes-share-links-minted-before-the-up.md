# R52. Removing a check name makes share links minted before the upgrade return 500 instead of 401

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-17#2 |

## Finding

- **Location:** `src/elspeth/web/execution/schemas.py:34-64`, `shareable_reviews/service.py:598-649,692-711` and `routes.py:175-184`.
- **Wrong:** 364801194 dropped `managed_identity_policy` from the `ValidationCheckName` Literal. `AuditReadinessSnapshot.model_validate_json` then raises an uncaught pydantic `ValidationError` for older blobs. Links live 30 days (`config.py:684`), and the payload store and signing key survive a session-DB recreate.
- **Fix:** Catch the `ValidationError` in `resolve_token` and raise `InvalidToken`, or version the blob.
- **Sources:** be-17#2.
- **Verifier notes:** Every blob minted since c9f3fe165 (2026-06) carries the row, not only those since 471d867fb. The new required `note` on `ValidationReadinessBlocker` (6a34007fb) causes the same failure for any older blob with blockers.


## Source findings and verification

### be-17#2: Removed check name makes pre-upgrade share links 500 instead of 401

- **Reported at:** `src/elspeth/web/execution/schemas.py:44`; reviewer severity medium; category error-handling; diff-anchored True.
- **Summary:** Dropping managed_identity_policy from the ValidationCheckName Literal makes AuditReadinessSnapshot.model_validate_json raise an uncaught pydantic ValidationError for share blobs minted before 364801194. _parse_blob only checks top-level keys, and the route catches only InvalidToken and PayloadNotFoundError.
- **Failure scenario:** A share link minted between 471d867fb and 364801194^. Its blob passes _BLOB_KEYS, and its validation_result.checks includes a passing managed_identity_policy row. After the upgrade the recipient opens the link and the token and digest verify. shareable_reviews/service.py:635 then raises a ValidationError (literal_error), which becomes an unhandled 500 instead of the designed 401 'unsupported share snapshot shape' or 404 'ask for a fresh link'. Links live 30 days, and the payload store and signing key survive a session-DB recreate.
- **Evidence:** shareable_reviews/service.py:692-711: _parse_blob validates only frozenset(parsed) == _BLOB_KEYS and its docstring says older blob shapes get no fallback reader. routes.py:175-184 catches only InvalidToken and PayloadNotFoundError. No app-level ValidationError handler exists (app.py only handles RequestValidationError). Runbook staging-session-db-recreation.md:121 and :309 say the payload store is never deleted and the signing key is independent. config.py:684 sets a default lifetime of 30 days.
- **Suggested fix:** In resolve_token, catch pydantic ValidationError from the audit_readiness and composition_snapshot parses and raise InvalidToken, or version the blob and reject by version in _parse_blob.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. I traced the path myself and it is reachable. 364801194 is inside the window and removes "managed_identity_policy" from the ValidationCheckName Literal (schemas.py:34-64; that Literal is the one the finding cites at :44). ValidationCheck.name is typed with that Literal under the strict, extra="forbid" base model (schemas.py:155, 186).

Before the removal, validate_managed_identity_policy emitted a PASSING ValidationCheck(name=CHECK_MANAGED_IDENTITY_POLICY) row for every composition that reached that phase (364801194^:_validation_materialization.py:627-637). A mark-ready composition's snapshot therefore carries that row. The check has existed since c9f3fe165 (2026-06-22), so the exposure is every blob minted from June up to 364801194, not only blobs minted after 471d867fb.

resolve_token (service.py:598-649) does four things in order. It verifies the token with the config key (app.py:660, which survives a session-DB recreate). It retrieves the blob. It runs _parse_blob, which checks only the top-level key set, the username and the compartment (service.py:692-711). It then calls AuditReadinessSnapshot.model_validate_json on validation_result (audit_readiness/models.py:90) at service.py:635. That call raises a pydantic ValidationError (literal_error on checks[i].name). The route catches only InvalidToken and PayloadNotFoundError (routes.py:175-184). None of app.py's handlers covers a pydantic ValidationError: they handle RequestValidationError (:2080) and OSError (:2039), among others. The result is a 500.

The _parse_blob docstring (service.py:696-698) says an older blob shape gets no fallback reader. Its explicit mapping of unsupported shapes to InvalidToken shows the intended answer is a 401, so the 500 is a real mismatch with that intent, not a recorded ruling.

Supporting evidence that the class is wider: 6a34007fb, also in the window, adds a REQUIRED `note: str | None` to ValidationReadinessBlocker (schemas.py:259-262). Any pre-upgrade blob with a non-empty readiness.blockers list fails the same way.

Why I lower the severity: the design already makes these links unusable ("no fallback reader"). The user-visible difference is a generic 500 with a server stack trace instead of a 401 "Invalid or expired share token". No data is exposed or corrupted, and the owner has to mint a new link in either case.
