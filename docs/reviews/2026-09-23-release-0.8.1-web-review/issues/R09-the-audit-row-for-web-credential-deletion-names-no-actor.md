# R09. The audit row for web credential deletion names no actor

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-03#3 |

## Finding

- **Status and severity:** confirmed, **medium**.
- **Location:** `src/elspeth/web/auth/admin_routes.py:240`, `app.py:1056-1066`, `web/auth/audit.py:1029-1046` and `coordination/identity_authority.py:545-551`.
- **What is wrong:** `delete_user` passes only the username and reason. `_record_retirement` writes `identity_disabled` with `metadata.actor='operator'`, no `actor_identity_id` and null request fields. That was defensible while the only deleters were the one dev admin or the CLI. Since ce3602aed, any of N admins can delete. `record_identity_disabled` (`audit.py:1377-1398`) does carry `actor_identity_id`.
- **Failure scenario:** With admins A and B, "jane" is deleted. `auth_events` shows `actor 'operator'` and no `request_id`. Only the structlog line (`admin_routes.py:257-263`) identifies the admin.
- **Suggested fix:** Thread `admin.user_id` and the request through `LocalAuthProvider.delete_user`, `RetireIdentity` and `IdentityRetired`, and record them the way `record_identity_disabled` does. Keep `'operator'` only for the CLI path. Also correct the `record_identity_retired` docstring, which says the deleting surface "is the OPERATOR and has no request to read".
- **Sources:** be-03#3.
- **Verifier notes:** `retire_identity` also emits `IdentityAuthorityRevoked` with `actor_identity_id=None`.


## Source findings and verification

### be-03#3: Web credential deletion's retirement audit row names no actor although many admins can now delete

- **Reported at:** `src/elspeth/web/auth/admin_routes.py:240`; reviewer severity medium; category audit-integrity; diff-anchored True.
- **Summary:** delete_user passes only the username and reason to the provider. _record_retirement writes identity_disabled with metadata.actor='operator', no actor_identity_id and no request fields. That was defensible when the only deleters were the single dev admin or the CLI. After ce3602aed, any of N administrators can delete, and the Landscape cannot say which one did.
- **Failure scenario:** Admins A and B exist. The account 'jane' is deleted with reason 'left the team'. auth_events shows identity_disabled, cause credential_deleted, actor 'operator', no request_id. The deleting admin appears only in the structlog line at :257-263.
- **Evidence:** admin_routes.py:240; app.py:1056-1066; audit.py:1029-1046 ('actor': 'operator', request fields None); identity_authority.py:545-551 IdentityRetired ('no actor identity to name'); by contrast audit.py:1377-1398 record_identity_disabled carries actor_identity_id.
- **Suggested fix:** Thread admin.user_id and the request through LocalAuthProvider.delete_user, RetireIdentity and IdentityRetired, and record them as record_identity_disabled does. Keep 'operator' only for the CLI path.
- **Verifier (trace):** upheld, confidence high, severity medium. I traced the path at 74c0ce0db and the finding holds. Since ce3602aed, `_require_local_account_admin` lets two kinds of caller use DELETE /api/auth/admin/users/{user_id}: the configured dev_admin_user and any identity in `authority.active_human_admin_ids`. Before that commit only the single configured dev admin could, so the deleter could be worked out from configuration. Now it cannot. The route hands only (user_id, reason) to `LocalAuthProvider.delete_user`. No actor, admin id or request goes into the retirement chain: not through `retire_identity`, not through `IdentityRetired`, and not through `record_identity_retired`, which hardcodes metadata actor='operator' and sets request_id, client_host and user_agent to None. `retire_identity` also emits `IdentityAuthorityRevoked` with actor_kind='operator' and actor_identity_id=None. The only thing that names the admin is the structlog line, and structlog is not the audit trail. I found no ruling in docs/agents/recent-code-hints.md that accepts this. The reason text the admin types, recorded as 'local credential deleted: <words>', does not reliably identify the deleter. The frontend calls this route from client.ts:588, so the path is live. One more point: the `record_identity_retired` docstring says the deleting surface 'is the OPERATOR and has no request to read'. For the web route that is now false, because that route has both a request and an authenticated admin. Medium severity is right: this is an attribution gap in the audit trail, and no data is corrupted.
