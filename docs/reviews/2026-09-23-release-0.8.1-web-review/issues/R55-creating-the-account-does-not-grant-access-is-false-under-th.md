# R55. "Creating the account does not grant access" is false under the default open registration (pre-existing)

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | People and access |
| Review line | seams |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-05-people-auth#3 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/admin/AddPersonForm.tsx:105`, `local.py:1183,1229` and `app.py:1116-1119`.
- **Wrong:** An admin-created account admits itself as ACTIVE on first sign-in when `registration_mode='open'`.
- **Fix:** Word the copy from `registration_mode`, which is already on `/api/auth/config` (`routes.py:159`), or pre-provision the account as pending.
- **Sources:** seam-05-people-auth#3.
- **Verifier notes:** The copy dates from a767c7767. ce3602aed widened who sees it.


## Source findings and verification

### seam-05-people-auth#3: 'Creating the account does not grant access' is false under the default open registration

- **Reported at:** `src/elspeth/web/frontend/src/components/admin/AddPersonForm.tsx:105`; reviewer severity medium; category ui-contract-mismatch; diff-anchored False.
- **Summary:** An admin-created local account self-admits as ACTIVE on first sign-in when registration_mode is 'open' (the default), because ensure_identity is called with activate=registration_mode=='open'. The form tells the administrator that access still has to be set up, and PeopleCapabilities does not carry registration_mode, so the UI cannot tell which is true.
- **Failure scenario:** An admin creates an account for someone, intending to approve access later, and hands over the password. That person signs in and works immediately with the default quota, and no administrator ever set up access.
- **Evidence:** local.py:1183 _login_sync calls _admitted_identity_id. local.py:1229 calls _admit, which calls the app.py:1116-1119 _admit_identity with activate=settings.registration_mode == 'open'. config.py:224 default is 'open'. Plan people-access-panel.md, 'Add a local person' step 4. ce3602aed widened the audience from the dev admin to all local identity admins.
- **Suggested fix:** Expose registration_mode (or an 'accounts self-admit' fact) in PeopleCapabilities and word the copy from it. Otherwise pre-provision the identity as pending when an admin creates the account.
- **Verifier (trace):** upheld, confidence high, severity low. The finding holds. The form's copy is false under the default open registration mode. Creating the account writes only the credential and never creates an identity row. On first sign-in, `_admitted_identity_id` calls `ensure_identity` with activate=(registration_mode=="open"), and "open" is the default. The new identity is therefore created ACTIVE and the person gets a working token without any administrator setting up access. Two parts of the finding are overstated. First, the UI can tell which is true: the public /api/auth/config response already carries registration_mode (routes.py:159, types/index.ts:26), even though PeopleCapabilities does not. Second, the practical impact is small. In open mode anyone can self-register and self-admit through /register anyway, so this does not bypass access control. The real harm is an administrator who believes they are gating access when they are not. I lowered the severity from medium to low. It is correctly marked diff_anchored=false: the copy dates from a767c7767, before the window. ce3602aed, inside the window, widened who sees it.
