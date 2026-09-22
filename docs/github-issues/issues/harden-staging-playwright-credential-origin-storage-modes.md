---
title: Staging Playwright setup trusts an unvalidated origin and stores its bearer token permissively
labels: [area/tests, area/web, type/bug]
---

The staging Playwright configuration sends a real account's credentials to whatever origin the environment names, writes the returned bearer token to disk with default permissions, and selects the specs that run against staging with a deny-list rather than an explicit inventory.

## What happens

**Unvalidated base URL.** `src/elspeth/web/frontend/tests/e2e/setup/staging-global-setup.ts:72` and `src/elspeth/web/frontend/playwright.staging.config.ts:36-41` check only that `STAGING_BASE_URL` is non-empty. Any value Playwright's request context accepts — including a plain-http origin — is then used as the destination for a credential POST. `ignoreHTTPSErrors` is separately switchable from the environment (`playwright.staging.config.ts:70`).

**Live credentials, permissively stored token.** The global setup posts `STAGING_USERNAME` and `STAGING_PASSWORD` to `/api/auth/login` (`staging-global-setup.ts:38-40`) and writes the returned `access_token` into a storage-state JSON file with `writeFileSync(path, …, { encoding: "utf-8" })` (line 65). There is no restrictive file mode and no exclusive creation, and the parent directory is created with `mkdirSync(…, { recursive: true })` (line 64), which is content with whatever is already there. A pre-existing file or symlink at the storage-state path is followed rather than refused. The setup also logs the username and origin (lines 77-79).

**Test inventory is a deny-list.** The staging config selects specs with `testDir` plus `testIgnore` (`playwright.staging.config.ts:44,48`) rather than an explicit `testMatch` inventory. A newly added spec is therefore opted in to the staging run by default.

## Impact

This harness runs against a deployed environment using a real account. The bearer token sits on the runner's disk at default permissions for the life of the run and afterwards, and the deny-list means the set of specs that touch a shared deployment changes whenever anyone adds a spec file — without that being a decision.

## Fix

- Validate `STAGING_BASE_URL` as an absolute URL and require https, allowing http only behind an explicit opt-in.
- Create the storage-state file with an explicit restrictive mode and exclusive creation, so an existing file or symlink at that path is an error rather than a target. Reduce the log line so it does not carry the username.
- Replace `testIgnore` with an explicit `testMatch` inventory, so adding a spec to the staging run is a deliberate act.

Done looks like: a spec added to the e2e directory does not run against staging unless it is listed, and the storage-state file cannot be pre-created by another user on the host.
