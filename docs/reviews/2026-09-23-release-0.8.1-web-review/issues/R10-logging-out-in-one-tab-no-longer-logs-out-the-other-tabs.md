# R10. Logging out in one tab no longer logs out the other tabs

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-02#1 |

## Finding

- **Status and severity:** confirmed, **medium**.
- **Location:**
  - `src/elspeth/web/frontend/src/api/authSession.ts:25-29`.
  - `src/elspeth/web/frontend/src/api/client.ts:251-255`, the 401 gate.
  - `client.ts:98-108`: `authHeaders` reads the token from localStorage.
- **What is wrong:** 7d981d6cf logs the user out only when the 401'd request carried a Bearer token that equals both the store token and localStorage. After another tab removes `auth_token`, this tab's requests go out with no header. The 401 that comes back is then treated as "not ours". No `storage` listener watches `auth_token`, and `loadFromStorage` runs only on mount.
- **Failure scenario:** Alice logs out in tab A. Tab B keeps rendering her authenticated shell (sessions, composition, mailbox), and every request 401s indefinitely until a manual reload. Before the window, B logged out on the first 401. Only a tab with a live run websocket still recovers (`executionStore.ts:814-817`).
- **Evidence:** All eight cases in `client.auth-races.test.ts` are same-tab. The `parseResponse` docblock (`client.ts:186-190`) still promises that any 401 triggers logout.
- **Suggested fix:** Add a `storage` listener for `auth_token`: call `logout()` when the value becomes null, and call `loadFromStorage()` when it changes. Alternatively, treat a 401 on a request that carried no credential, or a different credential, as a lost login while this tab's store holds a token. Add a test that clears localStorage without touching the store, and update the docblock.
- **Sources:** fe-02#1.
- **Verifier notes:** Logout in tab A never contacts the server, so tab B has no other way back to a consistent state.


## Source findings and verification

### fe-02#1: Logging out in one tab no longer logs out the other tabs

- **Reported at:** `src/elspeth/web/frontend/src/api/authSession.ts:25`; reviewer severity medium; category regression; diff-anchored True.
- **Summary:** responseOwnsCredential logs out only when the request carried a Bearer token that equals both the store token and localStorage. authHeaders() reads the token from localStorage. So after another tab removes the token, this tab's requests go out with no header, get a 401, and never trigger logout.
- **Failure scenario:** Alice is signed in in tabs A and B. She logs out in tab A, which runs localStorage.removeItem('auth_token'). Tab B's store still holds token T1 and user alice. Its 30s mailbox poll, or any click, is sent with no Authorization header, and middleware.py:91-103 answers 401. authFetch recorded credential.token=null, so responseOwnsCredential is false and parseResponse skips logout(). Tab B keeps rendering alice's authenticated shell, with her sessions, composition and mailbox still on screen. Every request 401s indefinitely until a manual reload. Before the window, the check `token !== null` logged tab B out and cleared its caches.
- **Evidence:** client.ts:251-255 now gates logout on responseOwnsCredential(response, store.token). authSession.ts:27-29 requires credential.token !== null and credential.token === localStorage.getItem('auth_token'). client.ts:98-108 authHeaders reads localStorage. No 'storage' listener exists for auth_token: grep finds only preferencesStore.ts:470 (the intro key) and useTheme.ts:127. The 8 cases in client.auth-races.test.ts are all same-tab; none changes localStorage independently of the store. The parseResponse docblock at client.ts:186-190 still promises 'any API call returning 401 triggers authStore.logout()'. The WebSocket path (executionStore.ts:814-817) still logs out unconditionally, so only a tab with a live run stream recovers.
- **Suggested fix:** Add a window 'storage' listener on auth_token that calls logout() when newValue becomes null and re-runs loadFromStorage() when it changes. Alternatively, treat a 401 on a request that sent no credential or a different one, while this tab's store holds a token and the generation is current, as a lost login and log out. Add a regression test that clears localStorage without touching the store, and update the parseResponse docblock.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this finding and confirmed the failure path myself. Commit 7d981d6cf changed the 401 check. Before, the client logged out on any 401 while the store held a token. Now it logs out only when the request carried a Bearer token that matches both the store token and localStorage. A request sent after another tab has cleared localStorage carries no Authorization header, so its 401 never logs this tab out. Nothing else in the app picks up a logout made in another tab: no 'storage' listener watches auth_token, loadFromStorage runs only once on mount, and nothing reloads on focus or visibility change. Logout in tab A does not contact the server either. It only calls localStorage.removeItem, so tab B has no route back to a consistent state except a WebSocket 401 or a manual reload. The commit message and docs/agents/recent-code-hints.md record no ruling on cross-tab logout, so this is not a deliberate decision. Medium stays right: tab B leaks no data beyond what was already on its screen, but the tab is stuck showing a signed-in shell whose every request fails.
