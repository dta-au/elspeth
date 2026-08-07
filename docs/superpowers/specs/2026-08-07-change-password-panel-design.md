# Change password panel — self-service credential rotation for local auth

Date: 2026-08-07. Status: design, pending implementation plan.
Branch: `release/0.7.2`.

## Problem

A local-auth user has no way to change their own password. `auth.db` is
written only by `elspeth composer users add` / `remove` (`cli.py:1906+`),
which requires shell access to the host holding a `0600` file. There is no
API, no UI, and no route: `grep` for `change_password` / `reset_password`
across `src`, `tests`, and `docs` returns nothing.

The immediate motivation is a demo account whose issued password cannot be
rotated by the person using it.

## Deployment framing

OIDC/Entra is the production identity model. `auth.db` is the supported
backstop for deployments without an identity provider — small single-task
installations.

That framing carries one **load-bearing precondition**:
`docs/superpowers/specs/2026-07-08-aws-ecs-runtime-readiness-design.md:338-358`
records that local auth on EFS is safe *only* under a strict single-task,
single-process posture, because SQLite's byte-range locking diverges from
NFS-class filesystems. The mitigations it names hold today — `doctor.py`
never opens `auth.db`, and `local.py` sets no `journal_mode` PRAGMA, so the
default `DELETE` rollback journal is in use rather than WAL.

**That precondition is currently unenforced.** `resolve_deployment_state_mode`
forces PostgreSQL for `session_db_url` and `landscape_url` on ECS targets,
but `auth.db` is not in that contract (`deployment_contract.py:_database_dialect`
accepts only those two field names). Raising `desired_count` to 2 is a config
change that nobody would classify as an auth change, and nothing would stop it.
This design does not fix that; it records that the tier depends on it.

## Scope

A signed-in local user changes their own password.

**Non-goals**, each with the reason:

- **Operator reset of another user's password.** The local provider has no
  authorization concept to hang it on: `UserIdentity` carries only `user_id`
  and `username`, and `LocalAuthProvider.get_user_info` never populates
  `groups` (it defaults to `()`). The CLI is the operator path by design —
  it requires filesystem access to a `0600` file, a stronger check than any
  role claim `auth.db` could carry.
- **Forced password change on first login.** Needs a `must_change_password`
  flag and a gate in the login flow; separate work.
- **Forgot-password / reset-by-email.** This is the shaped follow-up for the
  SME tier. Local auth *already has the machinery* —
  `create_email_verification_token`, `email_verification_tokens`,
  `email_verification_outbox`, `publish_pending_email_verifications`. Reuse
  it rather than building a second outbox.
- **A password policy.** See "Password rules" below.
- **Anything under OIDC/Entra.** Those providers own the credential; ELSPETH
  only validates their tokens.

## Data model

One additive column on `auth.db`'s `users` table:

```
password_changed_at INTEGER   -- NULL = never changed
```

Nullable, so existing users are unaffected and no backfill runs.

### Required: `_ensure_schema` must take a write reservation

`_ensure_schema` currently opens a **deferred** transaction and does a
read-then-write: `PRAGMA table_info(users)` then a conditional
`ALTER TABLE` (`local.py:297-299`). Two processes can both observe the column
missing; the second `ALTER` then raises `duplicate column name`.

This is latent today — every live `auth.db` already has `email_verified`, so
that branch never fires. Adding `password_changed_at` **re-arms it** for
exactly one boot per database: the first boot after deploy.

The failure lands in `LocalAuthProvider.__init__`, which `app.py:1234` calls
during app construction. The symptom is not a failed password change — it is
**the web task failing to boot**.

Measured, 25 concurrent-boot trials per mode:

| `_ensure_schema` transaction | Boots failed |
|---|---|
| Deferred (current) | **25 / 25** — `duplicate column name` |
| `BEGIN IMMEDIATE` | **0 / 25** |

The column landed exactly once in every trial under both modes, so the fix
serializes rather than masking a double-add.

**`_ensure_schema` must use `self._connect(immediate=True)`.** The helper
already supports it (`local.py:262`) and `__init__` already uses it for the
reaper pass on the next line. This is a fix to existing code that this
feature makes reachable.

## Revocation

`authenticate()` already round-trips SQLite via `_user_exists`. That becomes
`_active_user_state(user_id) -> (email_verified, password_changed_at) | None`,
selecting one more column from the query it already runs — no extra query on
the request hot path. Then:

> reject the token when `iat < password_changed_at`

Four properties, each load-bearing:

- **Strict `<`, never `<=`.** The endpoint stamps `password_changed_at = now`
  and mints a token with `iat = now`. Under `<=` the user is logged out by
  their own success.
- **A single `now`.** `_change_password_sync` must compute `now = int(time.time())`
  **once** and pass it through as `_issue_token(..., issued_at=now)` — the
  parameter already exists. Two independent `time.time()` reads can straddle
  a second boundary and mint a token that is already revoked.
- **Fails closed on a missing `iat`.** If `password_changed_at` is set and the
  token carries no `iat`, reject. `elspeth-7b3a5515b3` was this bug class: a
  missing `original_iat` silently disabling the only revocation-like bound.
- **Refreshed tokens die too, by design.** `_refresh_sync` deliberately
  carries the *original* `iat` forward (`local.py:1007-1009`) so chain age
  accumulates. A long-lived refreshing session therefore still presents its
  pre-change `iat` and fails the check.

### `_refresh_sync` must reject, not mint

`_refresh_sync` returns `_issue_token(user_id, username, issued_at=original_iat)`.
After a password change, that mints a token whose `iat` is still the pre-change
value — `authenticate()` rejects it on the very next request, but the refresh
route would first return **200 with a dead-on-arrival token**.

`_refresh_sync` must check `original_iat < password_changed_at` itself and
raise `AuthenticationError` rather than succeeding into failure.

### Known limit

`iat` is second-granularity. A concurrent session that refreshes within the
same second as the change survives until its next request. Sub-second,
self-healing, and the alternative is fabricating an `iat`.

## Provider method

`LocalAuthProvider.change_password(user_id, current_password, new_password) -> str`

Async wrapper over `_change_password_sync` via `run_sync_in_worker`, matching
`login` and `refresh` — bcrypt is intentionally ~200ms and must not block the
event loop.

Inside one `_connect(immediate=True)` transaction:

1. `now = int(time.time())` — computed once, used for both the stamp and `iat`.
2. Re-verify `current_password` with `bcrypt.checkpw`. If the user has vanished
   mid-flight, run the dummy-hash path so timing stays flat (the `_login_sync`
   convention at `local.py:944-947`).
3. Hash the new password with a fresh `bcrypt.gensalt()`.
4. `UPDATE users SET password_hash = ?, password_changed_at = ? WHERE user_id = ?`
5. Return `_issue_token(user_id, username, issued_at=now)`.

`CredentialAuthProvider` (`protocol.py:40`) gains the method, keeping the
protocol honest about what routes may call.

## Route

`POST /api/auth/password`, mirroring `/login` (`routes.py:227-269`):

| Concern | Treatment |
|---|---|
| Provider gate | `settings.auth_provider != "local"` → **404** (the endpoint genuinely does not exist in that deployment) |
| Identity | `Depends(get_current_user)` — no username in the body |
| Rate limit | `Depends(check_auth_rate_limit)` — IP-keyed, matching every other auth route |
| Validation | `current_password` **and** `new_password` both through `bcrypt_password_bytes`; `new_password` also `has_visible_content` |
| Success | `TokenResponse` + `_mark_sensitive_auth_response_uncacheable(response)` |
| Failure | `_route_auth_failure(..., failure_category="invalid_credentials", failure_stage="password_change")` → 401 |

### Password rules

**Match register exactly**: non-blank plus bcrypt's 72-byte bound. Nothing more.

Register enforces no minimum length; enforcing one on change alone would be
incoherent between two surfaces that set the same credential. That local auth
has no password policy at all is a real gap for a supported SME tier — it is
filed as its own item to decide deliberately, not half-fixed on one endpoint.

## Audit

No schema change.

`auth_events` carries a database-level `CheckConstraint` on `event_type`
(`schema.py:2202`), and the shape validator checks tables, columns, indexes
and foreign keys — **not constraints**. A new event type would therefore pass
validation and then fail at `INSERT` on every pre-existing database. SQLite
cannot `ALTER` a `CHECK`; it requires a full rebuild of an append-only audit
table.

`issuance_path` is already the free-string discriminator, carrying `login`,
`register`, `email_verification` and `refresh`:

- Success → `record_token_issued(..., issuance_path="password_change")`
- Wrong current password → `record_auth_failure(failure_category="invalid_credentials", failure_stage="password_change")`

Both are true statements in the existing vocabulary. What this does not do is
name the credential change as its own event kind; that is a follow-up, not a
reason to rebuild an audit table inside a release branch.

## Frontend

```
components/auth/ChangePasswordDialog.tsx   (new)   current / new / confirm
stores/authStore.ts                        (edit)  authConfig + loadAuthConfig()
api/client.ts                              (edit)  changePassword()
components/common/UserMenu.tsx             (edit)  entry, gated on provider
components/common/AppHeader.tsx            (edit)  mount dialog, load config
```

`authStore.loginWithToken(token)` already stores a token and refetches the
profile — that is the "swap the token after a successful change" primitive,
so the current tab stays signed in with no new plumbing.

**Menu placement**: its own "Change password" entry, rendered only when
`/api/auth/config` reports `provider === "local"`. `UserMenu.tsx:39-42`
records a deliberate decision to name the existing entry "Composer
preferences" rather than "Settings" because the pane holds one thing; a
provider-gated entry that disappears under Entra is more honest than a hub
whose tab set changes shape by deployment.

**Config access**: `authConfig` today is local `useState` inside `LoginPage`
(line 244), which runs pre-auth. Add it to `authStore` and load it from the
header post-auth; **leave `LoginPage`'s own fetch alone.** Lifting it out of
a 500-line, heavily-tested login path mid-release buys nothing here. Two
callers of one cheap endpoint on different lifecycles; reversible later.

The dialog follows the existing focus-trap / Escape / focus-return
convention, including the trigger-refocus-before-unmount handoff documented
at `UserMenu.tsx:83-89` (`elspeth-bcd1a9b9b3` — that bug bit once already).

## Tests

Backend, `tests/unit/web/auth/`:

- success path returns a usable token
- wrong current password → 401 and an audit failure row
- blank and over-72-byte new password rejected at the boundary
- non-local provider → 404
- **an old token is rejected after the change**
- **the freshly issued token is accepted**
- **refresh of a pre-change token is rejected by `_refresh_sync`** (not merely
  by the next `authenticate()`)
- **a different user's token is unaffected by this user's change** — a check
  that reads the wrong row, or drops `WHERE user_id = ?`, passes every other
  test here
- concurrent `_ensure_schema` on a database lacking the column does not raise
- audit row carries `issuance_path="password_change"`
- rate limit applies

Frontend: dialog vitest plus the `src/test/a11y` axe pass; UserMenu provider
gating; authStore config load.

The bolded cases are the guard tests. A test that passes while guarding
nothing is the failure mode this list exists to avoid.

## Gates

Stays on `release/0.7.2`. Before handback:

- `pytest tests/` in full — not scoped; whole-tree AST gates need the default
  selection
- `elspeth-lints check`
- `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`

A new route accepting browser-supplied credentials is precisely what the
wardline gate exists for.

## Open scheduling question

There is already a reset path: `elspeth composer users remove` then `add`
restores the same identity, because `user_id` *is* the username
(`create_user(username, ...)`, `_issue_token(username, username)`). On the
ECS scenario stack `REGISTRATION_MODE=open` (`locals.tf:447`), so
self-registering a fresh account also works; the foundryside.dev box has
`REGISTRATION_MODE=closed` and needs the CLI.

If either unblocks the demo, this work is **not** a demo blocker and should
queue behind `elspeth-d1602e4b90` (g05) and `elspeth-afdf55a17c` (g11). If
the panel itself must be on the stack first, the backend slice can ship ahead
of the revocation column.

`delete_user`'s cascade past the foreign keys on `email_verification_tokens`
and `token_audit_intents` should be verified before `remove` is run against a
live demo account.
