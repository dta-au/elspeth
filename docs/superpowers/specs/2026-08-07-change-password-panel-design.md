# Change password panel — self-service credential rotation for local auth

Date: 2026-08-07. Status: design, pending implementation plan.
Branch: `release/0.7.2`.
Revision: 2 — incorporates five independent reviews (architecture, systems,
Python, quality, security). Findings that corrected this document are marked
**[rev2]** so the change is auditable rather than silent.

## Problem

A local-auth user cannot rotate their own password without operator
involvement or destroying their identity. The only paths today are
`elspeth composer users remove` + `add` (`cli.py:1906+`), which needs shell
access to a `0600` file on the host, or self-re-registration where
`REGISTRATION_MODE` is open. There is no API and no UI: `grep` for
`change_password` / `reset_password` across `src`, `tests` and `docs` returns
nothing.

The immediate motivation is a demo account whose issued password cannot be
rotated by the person using it.

## Deployment framing

OIDC/Entra is the production identity model. `auth.db` is the supported
backstop for deployments without an identity provider — small single-task
installations.

That framing carries one **load-bearing precondition**:
`docs/superpowers/specs/2026-07-08-aws-ecs-runtime-readiness-design.md:338-358`
records that local auth on EFS is safe *only* under a strict single-task,
single-process posture, because SQLite's byte-range locking diverges on
NFS-class filesystems. The mitigations it names hold today — `doctor.py`
never opens `auth.db`, `local.py` sets no `journal_mode` PRAGMA (so the
default `DELETE` rollback journal is in use, not WAL), and `app.py:1415-1433`
hard-refuses `WEB_CONCURRENCY > 1` or `--workers > 1`.

**That precondition is currently unenforced at the deployment layer.**
`resolve_deployment_state_mode` forces PostgreSQL for `session_db_url` and
`landscape_url` on ECS targets, but `auth.db` is not in that contract
(`deployment_contract.py:_database_dialect` accepts only those two field
names). Raising `desired_count` to 2 is a config change nobody would classify
as an auth change.

**[rev2]** This design still does not fix that, but it now records the
enforceable shape, because two findings below (the DDL race, and the write-lock
DoS) are *materially worse* once the invariant is violated:

- **In-app**: extend `deployment_contract.py` so `auth_provider == "local"`
  AND `deployment_target in EXTERNAL_POSTGRESQL_TARGETS` fails the contract.
  `settings.auth_provider` is already available at that layer.
- **In Terraform**: a `desired_count <= 1` precondition where
  `scenario_id == "A"`. `desired_count` is invisible to the application.

Filed separately; not a dependency of this feature.

## Scope

A signed-in local user changes their own password.

**Non-goals**, each with the reason:

- **Operator reset of another user's password.** The local provider has no
  authorization concept: `UserIdentity` carries only `user_id` and `username`,
  and `LocalAuthProvider.get_user_info` never populates `groups` (defaults to
  `()`). The CLI is the operator path by design — it requires filesystem
  access to a `0600` file, a stronger check than any role claim `auth.db`
  could carry.
- **Forced password change on first login.** Needs a `must_change_password`
  flag and a login-flow gate; separate work.
- **Forgot-password / reset-by-email.** The shaped follow-up for the SME tier.
  Local auth already has the machinery — `create_email_verification_token`,
  `email_verification_tokens`, `email_verification_outbox`,
  `publish_pending_email_verifications`. Reuse it rather than building a second
  outbox.
- **A password policy.** See "Password rules".
- **Anything under OIDC/Entra.** Those providers own the credential.

## Data model

One additive column on `auth.db`'s `users` table:

```
password_changed_at INTEGER   -- NULL = never rotated
```

**[rev2]** It must be added in **both** places `email_verified` appears —
the `CREATE TABLE` (`local.py:288-296`) *and* the conditional `ALTER`
(`:297-299`). A brand-new database runs `CREATE TABLE` then `ALTER` in the same
boot; if the column exists only on the `ALTER` path, fresh installs race too,
not just the first boot after deploy.

### Required: make the additive `ALTER` idempotent

`_ensure_schema` does a read-then-write in a **deferred** transaction:
`PRAGMA table_info(users)` then a conditional `ALTER TABLE`
(`local.py:297-299`). Two processes can both observe the column missing; the
second `ALTER` raises `duplicate column name`.

This is latent today — every live `auth.db` already has `email_verified`, so
that branch never fires. Adding `password_changed_at` **re-arms it**.

The failure lands in `LocalAuthProvider.__init__`, which `app.py:1234` calls
during app construction. The symptom is not a failed password change — it is
**the web task failing to boot**.

Measured, 25 concurrent-boot trials per mode, **on local disk**:

| `_ensure_schema` transaction | Boots failed |
|---|---|
| Deferred (current) | 25 / 25 — `duplicate column name` |
| `BEGIN IMMEDIATE` | 0 / 25 |

**[rev2] That measurement must not be read as closing the risk on the real
substrate.** Where the race is actually reachable — `desired_count ≥ 2`, hence
EFS multi-task — `BEGIN IMMEDIATE` depends on exactly the byte-range locking
the July spec says diverges there. `_get_conn` passes no `timeout`
(`local.py:249`), so a contended `BEGIN IMMEDIATE` raises `database is locked`
after the 5s default — the same boot failure, a different exception. The 25/25
figure is a stress result on ext4, not a deployment prediction.

Therefore, **two changes, in this order of importance**:

1. **Required — make the `ALTER` idempotent.** Wrap it in
   `except sqlite3.OperationalError`, swallowing **only** `duplicate column
   name`. This is correct under every locking regime and needs no measurement
   to justify. `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
   in the same method are already idempotent; the `ALTER` is the sole
   non-idempotent statement. This restores the method's existing property
   rather than adding a mechanism.
2. **Defence in depth — `_ensure_schema` uses `self._connect(immediate=True)`.**
   The helper already supports it (`local.py:262`) and `__init__` uses it on
   the next line. It genuinely helps the local-disk case and changes the
   transaction rule for schema-ensure generally, so it also covers the *next*
   additive column.

## Revocation

`authenticate()` already round-trips SQLite via `_user_exists`. It gains one
column from that same query — **no extra query on the request hot path**.

> reject the token when `iat < password_changed_at`

### [rev2] The `email_verified` gate stays in SQL

Revision 1 described `_active_user_state` as "selecting one more column from
the query it already runs". **That was wrong and dangerous**, and three
reviewers flagged it independently.

`_user_exists` (`local.py:1038-1041`) is:

```sql
SELECT 1 FROM users WHERE user_id = ? AND email_verified = 1
```

`email_verified = 1` is a **WHERE predicate, not a projected column** — the
rejection of unverified users in `authenticate()` is a side effect of the
query returning no row. There is no branch anywhere in `authenticate()` that
says "reject unverified". Returning `email_verified` in a tuple only makes
sense if the predicate is dropped and the check moves to Python, and a
refactor that drops it without adding the branch back **silently starts
accepting tokens for unverified accounts**.

That gate is load-bearing: `_restore_retryable_verification`
(`local.py:670-673`) runs
`UPDATE users SET email_verified = 0 WHERE user_id = ? AND email_verified = 1`,
reached from the reclaim sweep (`local.py:616-656`). The predicate on the
`authenticate()` hot path is what makes that quarantine bite on the next
request.

**Therefore the predicate stays in SQL.** The helper becomes:

```sql
SELECT password_changed_at FROM users WHERE user_id = ? AND email_verified = 1
```

returning `tuple[int | None] | None`. `fetchone()` distinguishes the two cases
without ambiguity:

- `None` → no active verified user → reject (identical
  `AuthenticationError("Invalid token")` as today — unverified, missing and
  revoked must be indistinguishable to the caller, or the refactor introduces
  an enumeration oracle).
- `(None,)` → active user, never rotated → no revocation check.
- `(epoch,)` → active user, compare against `iat`.

**`_refresh_sync` keeps its own separate query** (`local.py:997-1001`),
extended with `password_changed_at`. It deliberately runs unfiltered because
it must distinguish "User not found" from "Email verification required" for
its distinct error messages. Sharing one helper between the two callers is
what created the trap; they stay separate.

### The five load-bearing properties

**[rev2]** Revision 1 listed four and framed the list as exhaustive. It omitted
the case that applies to every user in the database on day one.

1. **NULL means never rotated — check it first.** `password_changed_at` is
   NULL for every existing user, and in Python `1234 < None` raises
   `TypeError`. `authenticate()` runs on *every authenticated request*, so an
   unguarded comparison is not a password-change bug — it is total auth outage
   on deploy.
2. **Strict `<`, never `<=`.** With the `now + 1` construction below, the
   caller's fresh token carries `iat == password_changed_at` and must survive.
3. **A single `now`.** `_change_password_sync` computes `now = int(time.time())`
   **once** and threads it to both the `UPDATE` and `_issue_token(issued_at=…)`.
   Two independent reads can straddle a second boundary and mint a token that
   is already revoked. Because both derive from one value, `iat ==
   password_changed_at` holds *by construction*, not by luck — which makes
   property 2 order-independent under future refactoring.
4. **Fails closed on a missing *or ill-typed* `iat`.** If `password_changed_at`
   is set and the token carries no `iat`, reject. **[rev2]** Extend to
   ill-typed: PyJWT accepts a numeric-string `iat`, so `"1000" < 1000` raises
   `TypeError` → 500. Parse it the way the two existing `iat` consumers already
   do — `type(value) is not int` (`routes.py:432`, `audit.py:140-146`), never
   `isinstance` (which admits `bool`), never a silent `.get()` default (that is
   an `R1` violation, `trust_boundary.py:65-72`).
5. **Refreshed tokens die, by design.** `_refresh_sync` carries the *original*
   `iat` forward (`local.py:1007-1009`) so chain age accumulates. A long-lived
   refreshing session therefore presents its pre-change `iat` and fails.

### [rev2] The stamp is `now + 1`, and so is the re-issued token

Revision 1 stamped `password_changed_at = now` and minted at `iat = now`,
with a "Known limit" claiming the only survivor was a concurrent *refresh*
within the same second, "sub-second, self-healing."

**Both halves of that were wrong.** A same-second refresh cannot survive —
it carries the strictly-earlier original `iat` and dies. The reachable case
was unstated and far worse:

`_login_sync` reads the password hash inside a short transaction that then
**closes** (`local.py:938-942`), runs `bcrypt.checkpw` **outside** any
transaction (`:949`), and only then calls `_issue_token`, which takes a
**fresh** `int(time.time())` (`:958`). So:

```
T-0.30  attacker POSTs /api/auth/login with the OLD password
T-0.29  _login_sync reads H_old; its transaction closes
T+0.00  victim's change commits: password_changed_at = 1000
T+0.10  checkpw(P_old, H_old) -> True
T+0.10  _issue_token: int(1000.10) = 1000 -> iat = 1000
```

`1000 < 1000` is False, so the token is accepted — and `_refresh_sync`
re-mints it at the same `iat` indefinitely, up to the 168h chain bound. A
~250ms–1s window per rotation yields a **seven-day session the password change
cannot revoke**, and it is not self-healing: only a second rotation or chain
expiry clears it. The attacker need not know when the rotation happens — they
spray old-password logins until it does, at 20/min/IP (`config.py:365`).

**Fix**: stamp `password_changed_at = now + 1` and mint the re-issued token
with `issued_at = now + 1`. The racing login's `iat = 1000` is then
`< 1001` → revoked; the caller's own token at `iat = 1001` survives strict `<`.

**Clock-step clamp**: stamp
`max(now + 1, existing_password_changed_at + 1)`. A backwards NTP step would
otherwise make every subsequently-minted token born revoked, with no recovery
path since operator reset and forgot-password are both non-goals.

**Residual, recorded not hidden**: the sliver where the attacker's bcrypt
straddles the second boundary *after* the commit. Closing it completely
requires a compare-and-swap in `_login_sync` — re-read `password_hash` under
`BEGIN IMMEDIATE` and require it to be byte-identical to the one just
verified. That touches the unauthenticated login hot path and is deferred as
a follow-up, not because it is wrong but because it is a wider change than
this feature. Do **not** apply an `iat = max(now, password_changed_at)` clamp
in `_login_sync` on its own — without the CAS it bumps a racing old-password
login onto the boundary and reopens the hole.

### [rev2] What revocation does not reach

- **Live WebSocket streams.** `/ws/runs/{run_id}` authenticates once via a
  single-use 30s ticket, then streams until the run ends. A pre-change token
  can mint a ticket up to the moment of the change and hold the stream after.
  Bounded — own-run progress events, read-only.
- **Non-interactive token holders.** `scripts/acceptance_battery.py:65,73`
  caches a bearer token to `credentials.json` and replays it across process
  invocations. It mints throwaway `battery-*` accounts, so there is no live
  collision — but an automation run sharing an account with an interactive
  user would now die mid-run where it previously would not.

### `_refresh_sync` must reject, not mint

`_refresh_sync` returns `_issue_token(user_id, username, issued_at=original_iat)`.
After a change that mints a token whose `iat` is pre-change: `authenticate()`
rejects it on the next request, but the refresh route would first return
**200 with a dead-on-arrival token**. `_refresh_sync` must check
`original_iat < password_changed_at` itself and raise.

## Provider method

**[rev2]** `change_password` lives on `LocalAuthProvider` only. **Do not touch
`protocol.py`.** The file's convention is that local-only self-service
credential operations type the provider concretely — `/register`
(`routes.py:301`) and `/verify-email` (`:361`) both use `LocalAuthProvider`,
while only the genuinely generic `/login` (`:247`) and `/token` (`:441`) use
`CredentialAuthProvider`. Revoking *other sessions* is a `password_changed_at`
guarantee no LDAP provider could honour, so it does not belong in a shared
protocol. The real gate is the string compare on `settings.auth_provider`
(ADR-032); it must **never** be "tidied" into an `isinstance` check against a
Protocol — structural typing lets an impostor pass.

`LocalAuthProvider.change_password(user_id, current_password, new_password) -> str`,
async over `_change_password_sync` via `run_sync_in_worker`.

### [rev2] bcrypt runs outside the write reservation

Revision 1 put both bcrypt operations inside one `_connect(immediate=True)`.
**That shape is a denial-of-service, not merely a style deviation**, and it
inverts the convention every other write path follows: `create_user` hashes
then connects (`local.py:361-362`), as do `register_open_user_with_audit`
(`:406-409`) and `register_email_verified_user` (`:731-733`); `_login_sync`
closes its transaction before `checkpw`.

Two bcrypts is ~400-800ms holding SQLite's RESERVED lock, serialising every
other writer. `sqlite3.connect` passes no `timeout` (`local.py:249`) so
queued writers block 5s then raise. Every blocked call occupies a slot in the
**shared 16-worker pool** (`async_workers.py:29`) — the same pool
`authenticate()` uses on every authenticated request. Roughly 27 concurrent
change requests, reachable well under the 20/min/IP limit, stall
authentication application-wide on a single-task deployment.

The corrected sequence:

1. Short read transaction: `SELECT password_hash FROM users WHERE user_id = ?`.
   Close it.
2. `bcrypt.checkpw` outside any transaction. If the user vanished mid-flight,
   run the dummy-hash path so timing stays flat (`local.py:944-947`).
3. **Reject a new password equal to the current one** — `bcrypt.checkpw(new,
   stored_hash)` → 400. This is not smuggled policy: without it, a same-value
   "change" stamps `password_changed_at`, kills every other session, and writes
   an audit record asserting a rotation that did not occur. It is an
   audit-truth rule.
4. `bcrypt.hashpw` the new password, outside any transaction.
5. `now = int(time.time())`; compute the clamped stamp.
6. `_connect(immediate=True)` and a **compare-and-swap**:
   `UPDATE users SET password_hash = ?, password_changed_at = ? WHERE user_id = ? AND password_hash = ?`
   with the old hash. `rowcount == 0` → a concurrent change won → raise rather
   than silently losing the update. This mirrors the rowcount-precondition
   pattern at `local.py:864-865`. **The CAS is what preserves the
   serialisation the single-transaction shape was buying** — it is not
   optional once bcrypt moves out.
7. Return `_issue_token(user_id, username, issued_at=stamp)`.

Lock hold drops from ~600ms to ~1ms.

## Route

`POST /api/auth/password`, mirroring `/login` (`routes.py:227-269`):

| Concern | Treatment |
|---|---|
| Provider gate | `settings.auth_provider != "local"` → **404** |
| Identity | `Depends(get_current_user)` — no username in the body |
| Rate limit | **[rev2]** IP-keyed `check_auth_rate_limit` **plus** a per-`user_id` limit of ~5/min |
| Validation | `current_password` **and** `new_password` through `bcrypt_password_bytes`; `new_password` also `has_visible_content` |
| Success | `TokenResponse` + `_mark_sensitive_auth_response_uncacheable(response)` |
| Failure | `_route_auth_failure(..., user=user, failure_category="invalid_credentials", failure_stage="password_change")` → 401 |

### [rev2] Rate limiting: per-user, not only per-IP

Every other auth route is *unauthenticated*, so IP is the only key available.
This one has an unspoofable `user_id`. IP-only keying lets an attacker holding
a stolen bearer token use the endpoint as an **online guessing oracle** for
`current_password` — 20/min/IP, unlimited across IPs — upgrading temporary
token possession into permanent credential possession. With no minimum
password length (below), that is a live risk.

**Buckets must be namespaced** — `f"ip:{host}"` / `f"user:{user_id}"`.
`ComposerRateLimiter._buckets` is one `dict[str, list[float]]` keyed by a bare
string (`rate_limit.py:44`), and because `user_id` *is* the username, on a
stack with `REGISTRATION_MODE=open` an attacker could register the username
`127.0.0.1` and share a bucket with that client.

### [rev2] Failure audit must be attributed

`_route_auth_failure` defaults `user=None` and then passes `user_id=None,
username=None` (`routes.py:156-176`). Omitting `user=user` records every
wrong-`current_password` attempt **unattributed** — destroying the one signal
that would surface the guessing attack above. `/login` has the excuse that the
principal is caller-supplied; this endpoint does not.

### Password rules

**Match register exactly**: non-blank plus bcrypt's 72-byte bound.

Register enforces no minimum length; enforcing one on change alone would be
incoherent between two surfaces that set the same credential. That local auth
has no password policy at all is a real gap for a supported SME tier — filed
as its own item, not half-fixed here. Note this materially raises the severity
of the rate-limiting finding above.

## Audit

No schema change to `auth_events`.

**[rev2] Correction.** Revision 1 said the shape validator "checks tables,
columns, indexes and foreign keys — not constraints." **That is false.**
`_REQUIRED_CHECK_CONSTRAINTS` (`database.py:592-598`) includes
`("auth_events", "ck_auth_events_event_type")`, enforced at `:1490-1491`.

The conclusion survives for a subtler reason: that enforcement is
`any(c["name"] == constraint_name for c in checks)` — **name-only, never
comparing the constraint's SQL text**. So a new `event_type` value would pass
validation and then fail at `INSERT` on every pre-existing database. It also
means a database whose constraint was redefined to permit anything passes
audit-integrity validation unchanged — **a live audit-integrity hole worth its
own ticket, independent of this feature.**

The real blocker is also wider than "SQLite cannot `ALTER` a `CHECK`":
`auth_events` lives in Landscape, which is forced to PostgreSQL on ECS, and
Postgres *can* alter a check. The actual gap is that Landscape has no
cross-dialect migration story for enum-like `CHECK`s — so the follow-up must
not be scoped as waiting for a SQLite feature that is never coming.

`issuance_path` is already the free-string discriminator carrying `login`,
`register`, `email_verification`, `refresh`:

- Success → `record_token_issued(..., issuance_path="password_change")`
- Failure → `record_auth_failure(..., user=user, failure_category="invalid_credentials", failure_stage="password_change")`

`_token_issued_metadata` already records `issued_at` from the token's `iat`
(`audit.py:154`), so an investigator can derive the exact revocation boundary
from a success row — which survives the `now + 1` construction, since the
fresh token's `iat` *is* the stamp.

### [rev2] Accepted risk: no audit intent

`register_open_user_with_audit` and `verify_email_and_issue_token` both commit
a durable `token_audit_intents` row inside the `auth.db` transaction, deliver
the Landscape write, then clear it — so no unaudited auth state survives. This
design does **not**, so a crash between the credential commit and the audit
write leaves a changed credential and a mass revocation with no audit record.

**Recorded as accepted risk** for this release, with a `structlog` warning on
audit-delivery failure, because adding the intent is *not* a drop-in:
`_reclaim_stale_token_audit_intents` branches `if issuance_path == "register"`
and sends **everything else** to `_restore_retryable_verification`
(`local.py:638-650`), which sets `email_verified = 0`. A naively-added
`password_change` intent would **quarantine the user's account** on the next
sweep. Adding one requires an explicit `password_change` arm whose
compensating action is "none — the credential change stands, re-emit the
audit".

Also unrecorded by design: `elspeth composer users add` writes no
`auth_events` row, so a CLI reset is invisible to the auth audit trail.

## [rev2] Rollback

The additive nullable column is forward-safe and downgrade-tolerant at the
schema level — old `INSERT`s omit it and nothing does `SELECT *` on `users`.
But old code does not read `password_changed_at`, so **rolling the deployment
back restores access to every token the change revoked**. The exposure is
bounded by `token_expiry_hours` (24h) and the 168h chain, not unlimited.

## [rev2] Accepted risk: attacker-first change

An attacker holding both a token and the password can change it first. The
victim's tokens die, their password stops working, and with operator reset and
forgot-password both non-goals, recovery needs `elspeth composer users remove`
+ `add` from a shell on the host. This is **not preventable at this endpoint** —
requiring `current_password` is already the correct control, and no
self-service design can prevent it. It is recorded because the feature converts
"account compromised" into "account compromised *and* locked out", and the CLI
recovery path must appear in operator documentation.

## Frontend

```
components/auth/ChangePasswordDialog.tsx   (new)   current / new / confirm
stores/authStore.ts                        (edit)  authConfig, loadAuthConfig, storage listener
api/client.ts                              (edit)  changePassword(), 401 retry
components/common/UserMenu.tsx             (edit)  entry, gated on provider
components/common/AppHeader.tsx            (edit)  mount dialog, load config
```

### [rev2] Cross-tab logout breaks the stay-signed-in promise

Revision 1 claimed the current browser stays signed in via `loginWithToken`.
It does not, across tabs:

- `authStore.ts:63` — `loginWithToken` writes the new token to `localStorage`.
- `client.ts:219` — a **global 401 interceptor** calls `logout()` on any auth
  failure.
- `authStore.ts:80` — `logout` does `localStorage.removeItem(TOKEN_KEY)`.
- **No `storage` event listener exists** anywhere in `authStore.ts`.

So a second open tab keeps its in-memory pre-change token, its next request
401s, and the interceptor wipes the token tab A just wrote. **Tab A is logged
out by tab B's revocation.**

Two changes: add a `storage` listener to `authStore` that adopts a newer token
rather than logging out, and make the 401 interceptor re-read `localStorage`
and retry once before logging out. The opt-out mechanism already exists —
`logoutOnUnauthorized: false` (`client.ts:159`, used at `:1785`).

**Menu placement**: its own "Change password" entry, rendered only when
`/api/auth/config` reports `provider === "local"`. `UserMenu.tsx:39-42`
records a deliberate decision to name the existing entry "Composer
preferences" rather than "Settings"; a provider-gated entry that disappears
under Entra is more honest than a hub whose tab set changes by deployment.

**Config access**: add `authConfig` to `authStore`, load it from the header
post-auth, and **leave `LoginPage`'s own fetch alone** (`LoginPage.tsx:244`).
Lifting it out of a 500-line tested login path mid-release buys nothing.

**Lost-response copy**: the change commits before the response is sent. If the
response is lost, the old token is already revoked. Dialog copy must tell the
user to retry with the **new** password.

## Tests

Backend, `tests/unit/web/auth/`. Bolded are guard tests.

Revocation mechanism (provider unit level, mirroring `TestAuthenticate` at
`test_local_provider.py:1100-1223`, reusing `_signed_local_token` at `:59`):

- **`password_changed_at IS NULL` — an old token for a never-rotated user
  still authenticates.** Guards the `TypeError` that would break every
  authenticated request on deploy. The single most important test here.
- **`authenticate()` rejects a token whose user has `email_verified = 0`**,
  set up by flipping the column directly. Guards the SQL-predicate gate
  against the refactor. No such test exists today — the ten `test_authenticate_*`
  cases cover expiry, deleted user, wrong secret and claim shapes only.
- **A single `now`** — drive `auth_local.time.time` with a stateful fake
  (`iter([1_700_000_000, 1_700_000_001])`). Correct code stamps and mints from
  one read, so both equal the clamped first value on every run; a double read
  fails on every run. Deterministic in both directions.
- **Fails closed on missing `iat`** — hand-craft a token omitting `iat` for a
  user whose `password_changed_at` is set. A buggy
  `if iat is not None and iat < stamp` passes every other test and only this
  one catches it.
- **Fails closed on ill-typed `iat`** — string and float.
- **An old token is rejected**, constructed at exactly `password_changed_at - 1`
  so it does boundary work rather than passing against a "reject anything older
  than N minutes" heuristic.
- **The freshly issued token is accepted**, pinning `iat == password_changed_at`.
- **A racing login's token is rejected** — mint at `iat = now`, stamp at
  `now + 1`, assert rejection. Guards the seven-day window.
- **`_refresh_sync` rejects a pre-change token** (not merely the next
  `authenticate()`).
- **A different user's token is unaffected** — catches a dropped
  `WHERE user_id = ?`.
- **A failed change does not stamp `password_changed_at`** — an implementation
  that stamps before verifying, or in a `finally`, passes every other test
  while letting any token holder nuke a user's sessions without the password.

Change mechanics:

- **Success persists** — not "returns a usable token". `_issue_token`
  (`local.py:957-967`) is a pure JWT mint that never reads the database, so a
  token-validity assertion passes even if the `UPDATE` never ran. Assert
  instead: `login(new_password)` succeeds, `login(old_password)` raises, and a
  raw-SQL read shows `password_changed_at` non-NULL.
- Concurrent double-change loses no update (guards the CAS).
- New password equal to current → 400, and `password_changed_at` unchanged.
- The "user vanished mid-flight" dummy-hash path.
- Clock-step clamp: a backwards `time.time()` does not lower the stamp.
- Refresh-chain age (168h) × password change: which rejection surfaces.

Schema:

- **Concurrent `_ensure_schema` on a database lacking the column does not
  raise.** Must **force** the interleaving, not hope for it — this race
  manifests only when both connections complete their `PRAGMA table_info` read
  before either commits. Use the delegating-connection-proxy idiom
  (`_CommitFailingConnection`, `test_local_provider.py:26-37`) with a
  `threading.Event` pair (`:224-239`): proxy `execute()` blocks on
  `PRAGMA table_info(users)` until a second, uninstrumented provider has
  completed `_ensure_schema` and committed, then releases. Against today's code
  this raises `duplicate column name` on **every** run, not most. Match the SQL
  prefix exactly — `__init__` runs the two reapers straight after
  `_ensure_schema` (`local.py:234-237`). While authoring, temporarily revert
  the fix and confirm the test flips red.

Route level (`test_routes.py` conventions):

- Non-local provider → 404.
- Wrong current password → 401, asserting exact `failure_category` /
  `failure_stage` **and a non-NULL `user_id`** via `_only_auth_event`.
- **Refresh after a password change → 401, not 200** — every other
  refresh-failure mode has a route-level companion
  (`test_token_refresh_missing_iat_rejected` et al, `test_routes.py:847-1006`);
  a provider-level pass does not prove the route's `except AuthenticationError`
  wiring.
- Over-72-byte `current_password` (not only `new_password`); blank
  `new_password`.
- Per-user rate limit trips independently of the IP limit.
- Success audit row carries `issuance_path="password_change"`.

Frontend:

- **Focus returns to the Account trigger when "Change password" is chosen** —
  replicating `UserMenu.test.tsx:58-66` verbatim for the new entry. That test
  cites `elspeth-bcd1a9b9b3`, a bug this component has already had once.
- `authStore` holds the fresh token after a successful change (the feature's
  headline promise).
- A second tab's 401 does not evict the fresh token (guards the `storage`
  listener).
- Wrong current password keeps the dialog open with an inline error.
- Double-submit guard during the ~200ms round-trip.
- Provider gating: no entry under `oidc` / `entra`.
- axe pass, per `src/test/a11y`.

Time is controlled with
`monkeypatch.setattr(auth_local.time, "time", …)` — the existing convention
(9 uses in `test_local_provider.py`). No real sleeps, no freezegun.

Do **not** write a test asserting the residual same-second window is closed —
it would either fail permanently or invite a fix via a fabricated `iat`. If
tested at all, pin the *accepted* shape: bounded to one grace window.

## Gates

Stays on `release/0.7.2`. Before handback:

- `pytest tests/` in full — not scoped; whole-tree AST gates need the default
  selection
- `elspeth-lints check`
- `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`

## Open scheduling question

`elspeth composer users remove` + `add` restores the same identity, because
`user_id` *is* the username. `_delete_user_rows` (`local.py:489-495`) deletes
children before the parent in FK-safe order, so the reset is clean;
`remove` prompts interactively unless `--yes`, and `display_name` / `email`
must be re-supplied. On the ECS scenario stack `REGISTRATION_MODE=open`
(`locals.tf:447`), so self-registration also works; foundryside.dev is
`closed`.

If either unblocks the demo, this work is **not** a demo blocker and should
queue behind `elspeth-d1602e4b90` (g05) and `elspeth-afdf55a17c` (g11).

## [rev2] Follow-ups this review surfaced, filed separately

1. **Name-only `CHECK` constraint validation** — `database.py:1490-1491`
   verifies a constraint exists by name without comparing its definition. An
   audit-integrity hole independent of this feature.
2. **Single-task invariant unenforced** — the `deployment_contract.py` +
   Terraform checks described under Deployment framing.
3. **No password policy for local auth** — no minimum length on any surface.
4. **`_login_sync` compare-and-swap** — closes the residual same-second window.
5. **`/api/auth/token` has no rate limiter**, unlike `/login`, `/register`,
   `/verify-email`. Pre-existing.
6. **Landscape has no cross-dialect migration story for enum-like `CHECK`s** —
   the real blocker behind the audit-vocabulary decision.
