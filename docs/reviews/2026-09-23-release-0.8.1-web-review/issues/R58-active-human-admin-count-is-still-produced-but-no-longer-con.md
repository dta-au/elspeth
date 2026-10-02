# R58. `active_human_admin_count` is still produced but no longer consumed, and related prose is stale

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | frontend, seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-05-people-auth#5, fe-04#4 |

## Finding

- **Location:** `src/elspeth/web/auth/people_routes.py:157-159,512-513,557`, `frontend/src/types/people.ts:73`, `client.ts:541-544`, `types/index.ts:45-47`, `SignInSection.tsx:82`, and the `dev_admin_*` event names at `admin_routes.py:203,220,258`.
- **Wrong:** 793d0f95a removed the only consumer, the panel-wide advisory, but a comment it added says "the count the advisory shows". The client and type comments still describe a dev-admin-only surface, and the log event names still say `dev_admin_*` for every admin.
- **Fix:** Drop or re-document the field, update the comments, and rename the events to `local_account_*`.
- **Sources:** seam-05-people-auth#5, fe-04#4.
- **Verifier notes:** The `types/index.ts` and `SignInSection:82` staleness predates the window. The events still carry the correct `actor`.


## Source findings and verification

### seam-05-people-auth#5: Orphaned wire fields and stale prose after ce3602aed/P2

- **Reported at:** `src/elspeth/web/auth/people_routes.py:157`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** PeopleListResponse.active_human_admin_count is still computed and served, but P2 removed its only consumer, and the comments at people_routes.py:157-159 and 512-513 still name the removed advisory. Other stale text: client.ts:541-544 says admin users are 'env-gated; 404 unless dev_admin_user', and 'dev admin only'. types/index.ts:45-47 says dev_admin 'reveals the dev user-management menu entry', but nothing reads it. SignInSection.tsx:82 treats 'the local account administrator' as a different person from the viewer. The dev_admin_* structlog event names fire for administrators who are not the dev admin.
- **Failure scenario:** A maintainer trusts the comments and keeps or extends active_human_admin_count and dev_admin as live contracts, or filters logs by 'dev_admin_password_reset' and concludes that the configured dev admin acted when another administrator did. An admin viewing a retired or missing local account is told that someone else manages local accounts and that the person 'signs in with a local account'.
- **Evidence:** grep active_human_admin_count in the frontend: only types/people.ts:73 and peopleTestFixtures.ts. grep dev_admin in non-test frontend: only types/index.ts:47. admin_routes.py:203,220,258 event names. PeopleAccessDialog diff removed setActiveAdminCount.
- **Suggested fix:** Drop active_human_admin_count, or re-document it. Update the client.ts, types/index.ts and SignInSection copy to describe local-auth administrators. Rename the log events to local_account_*.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute the core claim. 793d0f95a (P2, in the window) removed the only frontend consumer of PeopleListResponse.active_human_admin_count. That was the panel-wide banner and the activeAdminCount prop in PeopleAccessDialog and PersonDetail. The backend still computes and serves the field, and its comment at people_routes.py:157-158 still says the field exists for "the single-administrator advisory". The same commit wrote the new comment at people_routes.py:512-513 ("the count the advisory shows"), even though it deleted that advisory. Neither the P2 plan section nor recent-code-hints records a decision to keep the count. The plan says only "drop the panel-wide banner". So this is a half-wired leftover, not a ruling.

ce3602aed (in the window) switched the admin routes to _require_local_account_admin. That made two things stale: the client.ts:541-544 comment ("404 unless the backend's dev_admin_user names the current local-auth user", "dev admin only"), and the dev_admin_* log event names at admin_routes.py:203/220/258, which now also fire for other local-auth administrators.

Some sub-items are weaker than the finding says:
- types/index.ts:45-47 (dev_admin "reveals the dev user-management menu entry", but nothing reads it) was already stale at 7c986dc97, so the window did not cause it.
- The SignInSection.tsx:82 copy comes from a767c7767, which is outside the window.
- The log events still carry actor=admin.user_id, so filtering by actor stays accurate. Only the event name misleads.

None of this breaks behaviour. It is stale documentation plus a wire field nothing consumes, so low severity stands.

### fe-04#4: active_human_admin_count is produced but no longer consumed, and the new comment wrongly says an advisory shows it

- **Reported at:** `src/elspeth/web/auth/people_routes.py:512`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** The window removed the only frontend reader of PeoplePage.active_human_admin_count (the panel-wide advisory in PeopleAccessDialog). The backend still emits it (:557), the TS type still declares it (types/people.ts:73), and the comment added in the same commit says 'the count the advisory shows'.
- **Failure scenario:** A maintainer reading people_routes.py believes a UI advisory depends on active_human_admin_count and keeps or extends a field that nothing reads. Runtime behaviour is unaffected.
- **Evidence:** At 74c0ce0db, grep for active_human_admin_count in frontend/src finds only types/people.ts:73, types/identityAdmin.ts:34 (a different endpoint, unconsumed) and peopleTestFixtures.ts:38. At 7c986dc97 it also found PeopleAccessDialog.tsx:124 setActiveAdminCount(page.active_human_admin_count). The comment at people_routes.py:512 is a + line in 793d0f95a.
- **Suggested fix:** Drop the field from the PeoplePage model and TS type, or keep it with an honest comment. Either way, correct the :512 comment to refer only to the per-row sole_active_admin flag.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at 74c0ce0db. The panel-wide one-admin advisory was removed on purpose in the window, and a test says so. Nothing in production frontend code reads PeoplePage.active_human_admin_count any more. The backend still computes and returns it, and the comment added at people_routes.py:512 in 793d0f95a says a UI advisory shows this count, which is no longer true. Runtime behaviour is correct because the per-row sole-admin flag uses the same active_admin_ids read, so this is only a half-wired or stale comment. Low severity is right.
