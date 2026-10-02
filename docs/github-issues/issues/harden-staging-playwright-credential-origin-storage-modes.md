---
title: Staging Playwright setup trusts an unvalidated origin and stores its bearer token permissively
labels: [area/tests, area/web, type/bug]
---

The Playwright configuration that runs the browser tests against a deployed staging environment sends a real account's credentials to whatever origin the environment names, writes the returned bearer token to disk with default permissions, and decides which tests run against staging with a deny-list rather than an explicit list.

**Where this lives.** Two files: `src/elspeth/web/frontend/playwright.staging.config.ts` and `src/elspeth/web/frontend/tests/e2e/setup/staging-global-setup.ts`. The setup logs in once before the suite and saves a *storage state* — Playwright's JSON file holding the signed-in browser state — which every test then loads instead of logging in again.

## What happens

**Unvalidated base URL.** `staging-global-setup.ts:72` and `playwright.staging.config.ts:36-41` check only that `STAGING_BASE_URL` is non-empty. Whatever it holds becomes the destination for a credential POST, including a plain-http origin. `ignoreHTTPSErrors` is separately switchable from the environment (`playwright.staging.config.ts:70`).

**Live credentials, permissively stored token.** The setup posts `STAGING_USERNAME` and `STAGING_PASSWORD` to `/api/auth/login` (`staging-global-setup.ts:38-40`) and writes the returned `access_token` into the storage-state file with `writeFileSync(path, …, { encoding: "utf-8" })` (line 65). There is no restrictive file mode and no exclusive creation, and the parent directory is created with `mkdirSync(…, { recursive: true })` (line 64), which accepts whatever is already there. A file or symlink already sitting at the storage-state path is followed rather than refused. The setup also logs the username and origin (lines 77-79).

**Test inventory is a deny-list.** The config selects specs with `testDir` plus `testIgnore` (`playwright.staging.config.ts:44,48`) rather than an explicit `testMatch` list, so a newly added spec runs against staging by default.

## Impact

This harness runs against a deployed environment with a real account. It is invoked manually (`npm run test:e2e:staging`) and is not wired into any CI workflow, so the token is written to the disk of whichever machine runs it — at default permissions, for the life of the run and afterwards. The set of tests that touch a shared deployment changes whenever anyone adds a spec file, without that being a decision anyone made.

## Fix

Correct behaviour, in three parts:

- `STAGING_BASE_URL` is parsed as an absolute URL and required to be https, with http allowed only behind an explicit opt-in. A malformed or plain-http value fails before any credential is sent.
- The storage-state file is created with an explicit restrictive mode and exclusive creation, so an existing file or symlink at that path is an error rather than a target. The log line no longer carries the username.
- `testIgnore` is replaced by an explicit `testMatch` inventory, so adding a spec to the staging run is deliberate.

You would know it holds from three checks, none of which needs a staging deployment: set `STAGING_BASE_URL` to a plain-http origin and to a non-URL string and confirm the setup refuses both before posting anything; pre-create a symlink at the storage-state path and confirm the setup fails rather than writing through it; add an empty spec file and confirm it does not run.

Size: small and well contained — three edits across the two files named above, no design decision needed, and the checks above are quick to run locally. A reasonable first ticket for someone new to the codebase.
