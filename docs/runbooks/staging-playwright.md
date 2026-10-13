# Staging Playwright login and storage

The local-account staging suite uses
`src/elspeth/web/frontend/playwright.staging.config.ts` against an existing
deployment. Supply `STAGING_BASE_URL`, `STAGING_USERNAME` and
`STAGING_PASSWORD` through the operator's environment. Never put account
credentials or saved browser state in tracked files.

`STAGING_BASE_URL` must be a bare HTTPS origin, such as
`https://elspeth.example.gov.au` or `https://deployment.example.test:8443`.
A trailing root slash is accepted and removed. User information, non-root
paths, query strings, fragments, whitespace and plaintext HTTP are refused
before login. TLS certificate verification remains enabled for the login
request. Login does not follow redirects, including 307/308 redirects that
could forward the credential POST. Configure the final deployment origin
directly.

The browser and authenticated API helpers use the same canonical origin.
`PLAYWRIGHT_BACKEND_BASE_URL` is set automatically; if explicitly supplied,
it must name that same HTTPS origin. A conflicting backend fails setup.

Any legitimate deployment HTTPS hostname remains usable. This harness has
no independent deployment-host allowlist, so HTTPS validation does not prove
that an operator selected the intended host. Keep credential injection and
the origin environment under the same trusted deployment configuration.
An unattended workflow that accepts origins from an untrusted source needs
an independently configured expected-host policy before injecting secrets;
deriving that policy from `STAGING_BASE_URL` would provide no protection.

The suite writes its bearer token into
`tests/e2e/.auth/staging-user.json`. Setup requires real directory components,
refuses a pre-existing file symlink, and makes the auth directory private
(0700). It writes a randomly named, exclusively created 0600 temporary file,
flushes it and atomically replaces the storage-state entry. An existing
regular state file is replaced with a private inode, so repeated runs work
without retaining its old permissive mode. Publication never follows the
destination file symlink. These filesystem controls require POSIX semantics;
the checkout and its parent directories must remain trusted operator-owned
state. They do not isolate a hostile process running as the same OS user.

Login failures report status or a fixed diagnostic, without response bodies,
account names or tokens. Setup emits no successful credential/account log.
The bearer remains on disk for later Playwright workers, so remove this state
file after the run when it is no longer needed and protect any copied browser
traces/reports as test artifacts. Run only one staging suite per checkout.

The focused unit checks mock the Playwright request context and use disposable
filesystem state; they need no deployment or real credentials:

```bash
cd src/elspeth/web/frontend
npx vitest run src/test/stagingGlobalSetup.test.ts --maxWorkers=1
```

The standalone `npm run staging:tutorial-harness` uses the same origin
validator. Its native Node login and bearer API requests refuse redirects,
and its error diagnostics omit response bodies. It keeps its token in memory
and uses it in the browser's origin-scoped state; it does not write the
Playwright bearer file. Its summary omits the account name. The separate OIDC
staging config has its own authentication path.
