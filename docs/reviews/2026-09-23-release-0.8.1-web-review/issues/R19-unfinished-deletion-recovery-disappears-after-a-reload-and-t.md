# R19. Unfinished-deletion recovery disappears after a reload, and the new comment claims it survives one

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | frontend, seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-05-people-auth#4, fe-04#2 |

## Finding

- **Status and severity:** confirmed, **medium**. seam-05-people-auth#4 is medium; fe-04#2 is low.
- **Location:**
  - `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:39-44`: `deleting` and `reason` are `useState`, and the comment was added in 793d0f95a.
  - `SignInSection.tsx:65`: `unfinished = account === null && deleting !== null && identityRetired === false`.
  - `SignInSection.tsx:82`: the copy.
  - `src/elspeth/web/auth/people_routes.py:223`: `manage_credentials = account is not None`.
- **What is wrong:** The comment calls the reason field "the ask-again fallback when none is held (a reload)". After a reload, or after re-selecting the person, `PersonDetail` remounts (keyed at `PeopleAccessDialog.tsx:264`), `deleting` is null, and the Finish-removal form never renders. The server still exposes the half-deleted state: provider local, `local_account` null and `retired` false. Line 82 then tells the local-account admin that someone else manages local accounts.
- **Failure scenario:** The credential delete commits, then the retirement or audit transaction fails with a 500. The admin reloads. The person is still live with their roles and no credential, and no UI path finishes the removal (AccessSection offers only approve, enable and disable). Under open registration, the next registration of that username binds to the live identity and inherits its roles (`local.py:650-671`). Only the CLI can finish the removal.
- **Suggested fix:** Derive "unfinished" from server state: provider local, no account, not retired, and the caller holds `local_accounts`. Offer Finish removal with a reason field. At minimum, correct the comment and the commit's claim.
- **Sources:** seam-05-people-auth#4, fe-04#2.
- **Verifier notes:** The state-only design predates the window. The window added the false comment. The empty-reason fallback is reachable only within the same mount: the delete fails, Cancel clears the reason, and a later re-read shows the account gone.


## Source findings and verification

### seam-05-people-auth#4: Unfinished-deletion recovery disappears after a reload, and the new comment claims a reload fallback

- **Reported at:** `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:40`; reviewer severity medium; category unreachable-code; diff-anchored True.
- **Summary:** The comment added in 793d0f95a says the reason field is 'the ask-again fallback when none is held (a reload)'. Both `deleting` and `reason` are component state, so after a reload `deleting` is null, `unfinished` is false and the Finish-removal form never renders. The server still exposes the half-deleted state (provider local, local_account null, retired false, local_accounts true), but the UI offers no recovery, and line 82 tells the local account administrator that someone else manages local accounts.
- **Failure scenario:** The credential delete commits, then the retirement or audit transaction fails (reproduced in people-access-implementation-review.md §5). The admin closes or reopens the panel. The person is still live with their roles and no credential, and the UI shows no way to finish the removal. Under open registration, the next registration of that username binds to the live identity and inherits its roles, per local.py delete_user's own docstring; only the CLI can finish.
- **Evidence:** SignInSection.tsx:39-44 declare useState-only deleting/reason and the new comment. :65 unfinished = account === null && deleting !== null && identityRetired === false. :82 copy. people_routes.py:223 manage_credentials = account is not None. local.py delete_user docstring on inherited identities.
- **Suggested fix:** Derive 'unfinished' from server state (provider local, no account, not retired, caller has local_accounts) and offer Finish removal with a reason field. Otherwise correct the comment and commit claim.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this finding. At 74c0ce0db the Finish-removal recovery depends entirely on component-local state. `deleting` is useState (SignInSection.tsx:39) and is set only by the delete-form submit (:124). `unfinished` is computed at :65 as `account === null && deleting !== null && identityRetired === false`. Nothing in the parent or the server can rebuild it: PersonDetail.tsx:206 passes only person.local_account, person.retired and actions.manage_credentials. On the server, manage_credentials is `account is not None` (people_routes.py:223), and the account lookup at :426 returns None once the credential is gone. So after a reload or a remount, a half-deleted person (provider local, local_account null, retired false) falls into the `account === null` branch. The form is never shown, and :82 says local accounts are managed by the local account administrator, which is the caller. The comment at :40-43 was added in window commit 793d0f95a. It calls the field 'the ask-again fallback when none is held (a reload)', and the commit message says 'asks again when none is held'. Both claims are false for the reload case. The empty-reason fallback is reachable only in a narrow same-mount sequence: the delete fails, the user presses Cancel, and cancelDelete clears the reason while `deleting` stays set. No UI path retires the identity: AccessSection offers only approve/enable/disable (:49). local.py delete_user's docstring (:650-671) confirms that the half-state lets the next registration of that username inherit the identity, and that a re-run of the removal is the intended recovery. Here only the CLI can do that re-run.

One caveat: the `deleting` useState and the reload blind spot existed before this window (in 793d0f95a's diff the `deleting` lines are unchanged context). What the window added is the false comment and the matching commit claim. The finding is still diff-anchored and correct. I kept medium because the half-state needs a failure between two commits, which is rare, and the CLI can recover. But the consequence, role inheritance by a later registrant, is authz-relevant, and the UI misleads the one admin who could act.

### fe-04#2: Comment promises an ask-again-after-reload path that cannot happen

- **Reported at:** `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:40`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** The new comment says the reason field 'is the ask-again fallback when none is held (a reload)'. reason and deleting are both component state and reset together, and unfinished requires deleting !== null, so after a reload the retry form never renders at all.
- **Failure scenario:** A deletion's retirement fails (500) and the administrator reloads the page or re-selects the person. The person has no local account and a live identity with roles, and the panel offers no Finish-removal control. The comment says the reason field is the fallback in exactly this case.
- **Evidence:** SignInSection.tsx:39 deleting useState, :44 reason useState, :65 unfinished = account === null && deleting !== null && identityRetired === false; PersonDetail is keyed on selected.key, so re-selecting remounts it.
- **Suggested fix:** Correct the comment. Better: derive 'unfinished' from server state (a live local-provider identity with no local account, and the manage_credentials capability) so the recovery survives a reload.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute it; the comment is wrong. Commit 793d0f95a added a comment at SignInSection.tsx:40-43 saying the reason field is "the ask-again fallback when none is held (a reload)". `reason` (:44) and `deleting` (:39) are both local useState, and neither is stored anywhere else. A reload, or re-selecting the person, remounts the component: PeopleAccessDialog.tsx:264 keys PersonDetail on selected.key. The remount resets `deleting` to null, so `unfinished` at :65 is false and the retry form at :69-79 never renders. The reason field therefore cannot be a fallback after a reload. There is a real case where the field asks again, but it is not a reload. The user submits a delete, which sets `deleting` at :124. The deletion fails while the account still exists. The user cancels, and cancelDelete (:59) clears `reason` but leaves `deleting` set. A later re-read then shows the account gone. That gives `unfinished` true with an empty reason. So the fallback exists, and the "(a reload)" example is the part that is false. The reviewer's failure scenario also holds: after a reload, a person whose account was deleted but whose identity was never retired gets no Finish-removal control. That limit comes from the design (deleting is only held in the component) and was there before this window. The new comment hides it by claiming a reload is covered. It is a comment defect only and changes no runtime behaviour, so low severity is right.
