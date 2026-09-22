# R59. The `active_human_admin_ids` docstring says "advisory for display", but the method is now the authorization gate for local-account admin

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-15#1 |

## Finding

- **Location:** `src/elspeth/web/coordination/identity_authority.py:1476-1478` and `admin_routes.py:134-158`.
- **Wrong:** 793d0f95a wrote the docstring. Seven hours later, ce3602aed made this read the only input to `can_manage_local_accounts`, which gates list, create, reset and delete.
- **Fix:** Document it as an authorization input: an unlocked per-request READ COMMITTED read. R5 is still decided inside the mutations.
- **Sources:** be-15#1.


## Source findings and verification

### be-15#1: active_human_admin_ids docstring says 'advisory for display' but it is the local-account admin authz gate

- **Reported at:** `src/elspeth/web/coordination/identity_authority.py:1477`; reviewer severity low; category stale-docstring; diff-anchored True.
- **Summary:** The docstring written in 793d0f95a says the read is 'Advisory for display: the mutations decide R5 for themselves, under their own lock.' ce3602aed (same window) then made it the sole authorization input of can_manage_local_accounts. That gates the create, reset and delete credential routes, and those mutations do not re-decide admin authority under any lock.
- **Failure scenario:** A maintainer trusts the 'display only' contract and caches, memoises or relaxes the read for the People panel, for example by dropping the access_state='active' or kind='human' filter from _ADMIN_HOLDER_ROWS. That silently changes who can create, reset or delete local credentials through /api/auth/admin/users, and nothing at the authz site signals it.
- **Evidence:** identity_authority.py:1471-1482 (docstring at 1477-1478); admin_routes.py:134-143 `return user.user_id in await run_sync_in_worker(authority.active_human_admin_ids)`; _require_local_account_admin Depends at admin_routes.py:168,189,212,229. Commit order: 793d0f95a 2026-09-21 01:36 wrote the docstring, ce3602aed 2026-09-21 08:35 adopted the read for authz. No ruling in docs/agents/recent-code-hints.md.
- **Suggested fix:** Rewrite the docstring to say this is also the authorization input for local-account administration (admin_routes.can_manage_local_accounts). Say it is an unlocked per-request READ COMMITTED read, so a revocation committed mid-request is seen on the next request, and that R5 is still decided inside the mutating transactions.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed the finding at 74c0ce0db. The docstring at identity_authority.py:1476-1477 still calls the read "Advisory for display", and blame shows it was written in 793d0f95a (2026-09-21 01:36). Seven hours later, ce3602aed (08:35) made this same read the only input to the non-dev-admin branch of can_manage_local_accounts (admin_routes.py:139-143). _require_local_account_admin (admin_routes.py:146-158) wraps that function, and it gates list, create, reset-password and delete on /api/auth/admin/users. None of those handlers checks admin authority again inside a lock or transaction. They go straight to provider.create_user, set_password or delete_user. The docstring's second clause ("the mutations decide R5 for themselves") is still true for role mutations, because R5 is the last-administrator rule. But "advisory for display" is now false: the read is an authorization gate for credential administration. Nothing guards against the failure path the finding describes. Anyone who narrows, caches or relaxes the read on the strength of the docstring changes who can administer local credentials. docs/agents/recent-code-hints.md has no ruling on this; its R5 hits are the unrelated tier_model lint rule. The only defect is a misleading comment, so severity stays low.
