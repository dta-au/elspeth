# R56. The hidden delete sub-view stays open and dirty after a partial deletion

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-04#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:61,74,125` and `peoplePanel.ts:84-96`.
- **Wrong:** After a 5xx delete followed by a successful Finish-removing retry, `confirm` stays `"delete"`. The new dirty predicate then raises "unsaved changes" prompts when no input is on screen, and Escape clears a reason the hint says is still held.
- **Fix:** Derive open and dirty from what is actually rendered, and close on retry success.
- **Sources:** fe-04#1.
- **Verifier notes:** Before the window this sub-view passed `dirty=false`.


## Source findings and verification

### fe-04#1: Hidden delete sub-view stays open and dirty after a partial deletion

- **Reported at:** `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:61`; reviewer severity low; category correctness; diff-anchored True.
- **Summary:** confirm stays "delete" when the first delete returns 5xx (ok=false), and neither the success path of the new Finish-removing retry form nor the account-gone branch resets it. The new useSubview dirty predicate (confirm === "delete" && reason.trim() !== "") therefore keeps a dirty sub-view with an Escape handler registered when no delete form is rendered.
- **Failure scenario:** Delete Jane's local account with the reason 'left the team'. The server deletes the credential and fails the retirement (500). Click 'Check current details', then 'Finish removing Jane Doe' (204); the person re-reads as retired. Clicking another person, 'Back to people' or the close button now shows 'You have unsaved changes. Discard them?' with no input on screen. Pressing Escape at the Finish-removing step runs cancelDelete, which clears the held reason under a hint that says 'This is the reason you gave'.
- **Evidence:** SignInSection.tsx:61 useSubview(`signin:${account?.username ?? provider}`, confirm !== null, confirm === "delete" && reason.trim() !== "", cancel); :74 retry run has no .then(close); :125 close() only if ok; peoplePanel.ts:84-96 registers escape and dirty from open/dirty alone; PeopleAccessDialog.tsx guard/requestClose prompt while dirtyOwners is non-empty. Before the window this sub-view passed dirty=false.
- **Suggested fix:** Derive the state from what is rendered: const deleteFormOpen = confirm === "delete" && account !== null && canManage; then call useSubview(owner, confirm === "reset" || deleteFormOpen, deleteFormOpen && reason.trim() !== "", cancel). Also call close() when the retry succeeds, or reset confirm when account becomes null.
- **Verifier (trace):** upheld, confidence high, severity low. I traced the path at 74c0ce0db and it holds. The partial-deletion state can happen: LocalAuthProvider.delete_user deletes the credential inside the retirer's transaction, and the retirement and audit write can then fail, which gives a 5xx. After that 5xx, `confirm` stays "delete" because close() only runs when ok is true (SignInSection.tsx:125). "Check current details" re-reads the person, who now has local_account=null and retired=false, so the component switches to the unfinished branch (:64-80). SignInSection has no key and sits at a fixed position in PersonDetail's fragment (PersonDetail.tsx:206), so its state survives the switch. The retry at :74 never resets `confirm` or `reason`. After the 204, the re-read returns the person as retired (IdentityPerson.retired, people_routes.py:121; retired identities are still returned, not 404), so unfinished is false and no form is shown. useSubview (:61) still gets open=true and dirty=true, so it registers an Escape handler and marks the owner dirty (peoplePanel.ts:88-95). PeopleAccessDialog then prompts "You have unsaved changes" on requestClose or guardLeave (PeopleAccessDialog.tsx:135,153,231). Before the window, dirty was hard-coded to false, so the stale prompt is a regression introduced in the window. Low severity is right: nothing is lost, it is a spurious prompt, and an Escape press cancels the leftover sub-view instead of closing the dialog.
