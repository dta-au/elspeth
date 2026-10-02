# R57. Moving Sign-in above the tabs places the Roles tab's content under the Sign-in heading

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-04#3 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/admin/PersonDetail.tsx:205-209` and `RolesEditor.tsx:108,125`.
- **Wrong:** For heading navigation, the revoke confirmation (`h5`) becomes a subsection of "Sign-in".
- **Fix:** Give the tab panel a heading, visually hidden if necessary, or add a "Roles" `h4`.
- **Sources:** fe-04#3.
- **Verifier notes:** A named `region` landmark mitigates this. Before the window the Roles content sat, headingless, under "Access".


## Source findings and verification

### fe-04#3: Sign-in moved above the tabs nests the Roles tab's content under the Sign-in heading

- **Reported at:** `src/elspeth/web/frontend/src/components/admin/PersonDetail.tsx:205`; reviewer severity low; category accessibility; diff-anchored True.
- **Summary:** With h4 'Sign-in' now directly before the tablist, the Roles tab panel, which has no heading of its own, and its new h5 'Revoke X from Y?' confirmation sit under 'Sign-in' in the heading outline, not under Roles.
- **Failure scenario:** A screen-reader user navigating by headings in the Roles tab hears 'Revoke Administrator from Jane Doe?, heading level 5' as a subsection of 'Sign-in'. Before the window, Sign-in came after the tab panel and the revoke confirmation was an h4 sibling.
- **Evidence:** PersonDetail.tsx:205-206 <h4>Sign-in</h4> before the tablist at :209; RolesEditor.tsx:108 <h5>Revoke …</h5> and first h4 at :125 (after it); RelationshipsEditor opens with its own h4 at :127, so it is unaffected. The tabpanel is aria-labelledby its tab, so only heading navigation is affected. recent-code-hints.md has no ruling on this.
- **Suggested fix:** Give the tab panel a heading (for example a visually hidden h4 naming the selected tab), or add a 'Roles' h4 inside RolesEditor above the grant list.
- **Verifier (trace):** upheld, confidence high, severity low. Could not refute. At 74c0ce0db, commit 793d0f95a moved `<h4>Sign-in</h4>` above the tablist. The Roles tab panel has no heading of its own, so its revoke confirmation, now demoted to h5 in the same commit, sits under "Sign-in" in the heading outline. The heading order was changed on purpose, but the result goes against the plan's own intent: P4 says to "Demote confirmation headings one level below their section", and the Roles section has no heading for the h5 to sit under. No ruling in recent-code-hints covers this. Severity stays low because of two mitigations. First, RolesEditor wraps its content in `<section aria-label="Roles for X">`, a named region landmark, and the tabpanel is labelled by its tab. Second, the path only occurs when the revoke form is open; with no form open the Roles tab has no headings at all. One correction to the finding's "before" picture: before the window the tabs sat after `<h4>Access</h4>`, so the Roles content was headingless then too and sat under "Access". The regression is that it now sits under an unrelated heading, and the confirmation became a subsection of it.
