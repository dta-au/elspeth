# Sharing Pipelines for Review

Operator runbook for the Phase 6 shareable-reviews feature. Covers the
deploy-time configuration, the lifecycle of a shareable link, and the
recovery procedure when a signing key is compromised or rotated.

## When to use this feature

A composer user with a valid composition state can mint a **shareable
link** that lets a different authenticated user view a frozen snapshot of
the pipeline and, only when the owner allows it, import a copy (below). The
reviewer sees:

* The composition graph (sources, transforms, sinks), as a redacted
  public projection.
* The rendered YAML, from the same public projection.
* All six rows of the Audit Readiness panel as they stood at mark-time
  (the `secrets` row's detail is withheld).
* Who shared the pipeline and when, plus the link's expiry.

The reviewer cannot edit, execute, or fork the shared session. The
inspect view is purely read-only.

The owner can also tick **Let recipients import a copy** when creating the
link. A recipient of such a link sees **Import a copy into my workspace**,
which creates a new session of their own holding a copy of the graph and of
every uploaded file it uses (see [Import-time](#import-time)). The shared
session is never modified. Links created without that option, and links
minted before it existed, stay view-only.

## Deployment requirements

### Required: `web.shareable_link_signing_key`

The web service refuses to start without this key configured. Generate
it once per deployment:

```bash
openssl rand -base64 32
```

The output is a 44-character base64 string (32 bytes of entropy). Store
the result in your staging / production configuration under
`web.shareable_link_signing_key` or set the environment variable
`ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` to the same value.

The key must:

* Be at least 32 bytes (base64-decoded). Shorter keys are rejected at
  service startup, as are string values that are not valid base64.
* Not appear in version control, logs, chat transcripts, or screenshots.
* Be different from any audit-signing key the deployment uses — the
  shareable-link key has a different rotation cadence and a different
  blast radius.

**Rotation invalidates every outstanding shareable link.** There is no
dual-key acceptance window in v1; once you rotate, every link a user
previously copied returns 401. Document the planned rotation cadence
with your operations team, and prefer NOT to rotate unless the key has
been leaked.

### Optional: `web.shareable_link_lifetime_seconds`

Default: **30 days** (`2592000`).

This is the longest lifetime a link can have. The owner picks 1 day, 7
days or 30 days for each link; the dialog offers only the choices at or
below this maximum and defaults to the longest of them. When none fits (a
maximum below one day) the dialog says so and the link lasts the maximum.
The server refuses a longer `lifetime_seconds` with 422, and re-minting
keeps the chosen lifetime, clamped to the current maximum. The signer's
`verify()` rejects tokens past their expiry regardless of their HMAC
signature. Operators may lower or raise this to match their review cadence;
values must be `> 0`.

Nothing in the web application deletes stored snapshots, or the files
frozen into a link that allows import, from the payload store: they remain
after the link expires (`payload_store_retention_days` does not apply to
them). Identical bytes are stored once. Reclamation is tracked in
[#356](https://github.com/dta-au/elspeth/issues/356). A snapshot or file
removed by other means, for example by `elspeth purge` of a run payload with
identical bytes, makes the link answer 404 with an "ask the sender for a
fresh link" message.

## First-deploy operator action

Since 0.8.1, shareable-review state is part of the broader web session
database contract. The unreleased 0.8.9 candidate expects `SESSION_SCHEMA_EPOCH=78`
and `SQLITE_SCHEMA_EPOCH=50`; confirm both against the deployed service's
live schema constants before a fresh installation.

Epoch 78 retains authoritative committed graph-review facts and adds durable quota
custody and shared rate-limit policy binding; Landscape epoch 50 binds run calls
to provider attempts. Preserve epoch-75, both distinct epoch-76 and epoch-77 session
store contracts and epoch-49 Landscape stores with their compatible prior application,
and provision separate epoch-78 Sessions and epoch-50 Landscape targets for the candidate.
Drain and isolate old/new workers; they must
not share these stores. Compatibility keys do not automatically evict old
replicas.

**Temporary deployment policy:** only fresh/new stores are supported until
explicit revision; this policy has no fixed expiry. Preserve existing stores:
do not delete, recreate, restamp, migrate or silently modify them to satisfy
the candidate. A stale store or predecessor transition is a stop condition,
not permission to reset it. An approved historical compatibility receipt cannot
override this policy.

### Historical schema changes

The following records explain earlier schema boundaries; they do not authorize
an existing-store transition under the current policy. Landscape epoch 30 adds durable
row_union barrier
attribution, and epoch 31 closes scheduler work-item status over the public
six-state vocabulary. Epoch 32 adds atomic, durable aggregation result receipts
for transform, passthrough, and empty outputs so recovery does not replay a
completed plugin call. Epoch 33 gives token outcomes a composite
(run_id, token_id) access path so run-scoped per-token reads resolve through
one index. Epoch 34 adds the unified-lineage groundwork tables
(`token_lineage_frames`, `group_records`, `group_losses`) and
`token_work_items.lineage_path_json`. Epoch 35 flips lineage onto that
groundwork: the tri-column `fork_group_id`/`expand_group_id`/`branch_name`
discriminators are retired and `token_lineage_frames`/`lineage_path_json`
become the sole lineage truth. Epoch 36 binds coalesce effects to their
non-null lineage group. Epoch 37 widens the auth provider CHECK constraints on
`auth_events` and `run_attributions`, and epoch 38 adds the
`scheduler_events.seq` replay key that orders scheduler transitions. Epoch 39
adds immutable web run-start permit binding and recoverable pre-effect admission
state. Session epoch 54 adds durable Composer progress snapshots and exact
request lifecycle leases. Session epoch 55 adds identity ownership constraints,
approval revocation provenance and durable admission decisions. Session epoch 56
preserves sparse proposal arguments and structured validation errors. Landscape epoch
40 adds call token measures and quota-policy/secret-wiring admission evidence.
Session epoch 57 and Landscape epoch 41 replace the fallback-template digest
with the approved prompt artifact anchor. Session epoch 58 widens the three quota
policy limits to 64-bit integers and makes token-ledger prompt/completion measures
nullable: unknown usage is NULL, never zero. Landscape epoch 42 requires
admission evidence v2 with per-principal token quota usage and limits; stored
v1 evidence is incompatible. Session epoch 59 adds timestamp-leading indexes for
container-wide quota scans. Session epoch 61 established freeform as the
Composer default; predecessor session databases do not match that schema.
Session epoch 62 requires nullable backend suggestions in durable advisor
completion gates and rejects older stores at startup before session reload.
Session epoch 63 makes the `blob_inline_resolutions.content_hash` CHECK and the
four `blob_replacement_cleanups` evidence-hash CHECKs enforce the full lowercase
SHA-256 shape rather than the length alone, and gives every remaining session
digest column its own shape CHECK. Landscape epoch 43 does the same for every
Landscape digest column.
It also adds `interpretation_events.surface_origin`: review cards raised by
the state-revert, YAML-import and E2E-seed routes now record that origin with
empty LLM provenance, where they previously wrote the route name into the
model, provider and `composer_skill_hash` columns.
A Landscape store below the deployed epoch is stale. Current SQLite and
PostgreSQL inspection does not transform a predecessor schema in place.
Preserve the store and stop deployment. The
[staging session DB recreation runbook](../runbooks/staging-session-db-recreation.md)
describes a historical reset procedure; it does not authorize deletion,
recreation, restamping or migration under the current fresh-store-only policy.

The shareable-link signing key is still required. Generate and configure it
before returning the service to users.

## Lifecycle of a shareable link

A shareable link goes through three distinct lifecycle phases.

### Mark-time

The owner performs **Save for review**, labelled **Share inspect link** in
the composer UI (`POST /api/sessions/{session_id}/mark-ready-for-review`).
The backend:

1. Runs validation against the current composition state. Validation
   failures return HTTP 409 — the user fixes the errors and tries again.
2. Computes the Audit Readiness snapshot. If any row's `status` is
   `error`, returns 409 — sharing a known-broken state is share-theatre.
3. **If import is allowed** (`allow_import: true`), reads every uploaded
   file the graph is attached to (a source's `blob_ref`, a `blob_rows`
   source's entries, transform inline-content markers) and checks each
   against its recorded hash and size. Only a ready file the user uploaded
   verbatim can travel; any other refuses the request with 409 naming the
   file. Stored bytes that fail their own hash check, or are missing from
   storage, fail the request with 500 instead. The request must carry
   `file_manifest_fingerprint`, the fingerprint the file preview returned
   (422 without it). If the files that would be frozen no longer match it,
   the request is refused with 409 (`error_type: share_files_changed`) and
   the dialog reloads the list so the owner confirms the current files. It
   also records each sink output path that lies inside the owner's session
   `outputs/` or `blobs/` folder.
4. Freezes the entire snapshot (composition + YAML + readiness panel, and
   for an import link the private file and sink-path manifests) into a
   canonical-JSON blob and computes its sha256 content-address.
5. **Stores each frozen file** in the payload store (import links only).
6. **Inserts an audit row** into `composer_completion_events_table` with
   `event_type='mark_ready_for_review'` (sync, crash-on-failure).
7. **Writes the blob** to the payload store, addressed by the digest.
8. **Signs a token** encoding `(session, state, expiry, digest, owner)`
   with HMAC-SHA256 over the canonical envelope.
9. Returns `{token, share_url, expires_at, payload_digest}` to the UI.

Before the owner confirms, the dialog reads
`GET /api/sessions/{session_id}/shareable-files`: the files an import link
would carry (name, size, where each is used), whether the graph can be
shared for import and why not, `max_lifetime_seconds`, and, when the graph
can be shared for import, `manifest_fingerprint`. The fingerprint covers
each file's content hash, size, name, type and every place it is used. It
judges each file from its stored record and reads no file content.

The audit row carries the digest and the expiry. The token carries the
same fields plus a random nonce that distinguishes two tokens minted at
the same instant.

### Re-mint-time (no new audit row)

`GET /api/sessions/{session_id}/shareable-link` mints a fresh token for
the current composition state's existing mark-time snapshot. It reads the
prior mark's `payload_digest` and requires that blob to still exist. The
token string and expiry are fresh, but it resolves to the same frozen blob.

No audit row or blob is written on re-mint, and readiness is not recomputed.
The original mark-time write remains the auditable decision.

If the current state has no mark or its marked blob is missing, re-mint
returns HTTP 409. The owner must **Save for review** again: this reruns
validation, completion and readiness gates, records a new audit event, and
stores a snapshot. Clicking **Share inspect link** invokes that mark-time
operation rather than the re-mint GET.

### Resolve-time

The reviewer follows the share URL. The frontend opens the SPA at the
hash route `#/shared/{token}` and makes a backend call to
`GET /api/sessions/shared/{token}`. The backend:

1. **Authenticates the reviewer.** The token is a CAPABILITY, not an
   authenticator — the reviewer must be logged in. Anyone with the URL
   but no account on the deployment gets 401.
2. **Verifies the token signature** via constant-time HMAC comparison.
   Any tampering, signature mismatch, malformed envelope, or expired
   `expires_at` returns 401 with a generic "invalid or expired" message
   (the failure mode is deliberately not disclosed).
3. **Reads the blob** from the payload store by `payload_digest`. If
   the blob has been reaped by the retention policy, returns 404 with a
   "ask the sender for a fresh link" message.
4. **Returns the frozen snapshot** including the mark-time readiness
   panel, as a redacted public projection of what the owner saw. All
   six readiness rows are present, but the `secrets` row's `detail` is
   nulled; the graph and YAML are `generate_public_composition_dict` /
   `generate_public_yaml` output, which excludes source and sink path
   carriers, nested transform persistence paths, blob identifiers, and
   bind-source markers.

The resolve path does NOT call `ReadinessService.compute_snapshot` —
the audit_readiness field is read directly from the frozen blob. This
guarantees the reviewer sees the owner's mark-time view, not a drifted
fresh-fetch view. For an import link the response also lists the frozen
files by name and size; it never carries their hashes, blob ids or paths.

### Import-time

On a link that allows import, the recipient clicks **Import a copy into my
workspace** (`POST /api/sessions/shared/{token}/import`). Any signed-in user
with the deployment-wide `user` role who holds the link can import it; the
request uses the same strict per-user rate limit as opening the link. The
backend:

1. **Verifies and reads everything first:** the token (401 if tampered or
   expired), the import opt-in (403 for a view-only link), the snapshot and
   every frozen file (404 "ask the sender for a fresh link" if one is gone).
   Nothing is created until these reads succeed.
2. **Creates a new session** owned by the recipient, titled "Copy of
   ‹pipeline name›".
3. **Copies each frozen file** into it as an ordinary upload, under the
   recipient's storage quotas (413 when over quota).
4. **Seeds the graph** through the ordinary YAML importer, with its credential,
   plugin-policy and redaction checks, re-attaching every file binding to the
   recipient's copy and re-basing each recorded sink path under the same
   folder of the recipient's session. A sink path outside the owner's session
   folders does not travel and must be set in the copy. A graph using a
   plugin the recipient may not use is refused with 422; the body carries no
   text, only `error_code`, `component_id` and `plugin_id`, from which the
   import page names the component and why it is unavailable. The YAML
   size cap and the credential check also answer 422, and the seeder's other
   refusals answer 400 with a plain-language detail.
5. **Records provenance** as `shared_import` in the new state's
   `composer_meta` (the snapshot digest, the sharer, and each file's copy).

Any failure after step 2 archives the new session. Each import creates a new
session; the owner is not notified and nothing is written on the owner's
side.

## YAML export audit

Every call to `GET /api/sessions/{session_id}/state/yaml` records an
`event_type='export_yaml'` row in `composer_completion_events_table`.
The row carries the actor (user_id), the composition_state_id, and the
timestamp. `payload_digest` and `expires_at` stay NULL — YAML export
does not mint a token or write a blob; the audit row is the entire side
effect.

Two YAML exports of the same state produce two distinct audit rows
(append-only audit table, no deduplication).

## Recovery procedures

### A signing key has been leaked

**Symptoms:** unauthorized share URLs appearing; an attacker has
unauthenticated access (note: they still need an account on your
deployment to call the inspect route — the token grants *which session*
they can read, not *whether they can read at all*).

**Procedure:**

1. **Generate a new signing key** with `openssl rand -base64 32`.
2. **Update the deployment configuration** with the new value (env var
   or config file).
3. **Restart the web service.** The service will load the new key and
   reject every outstanding token immediately.
4. **Notify users** that previously shared links have been invalidated.
   The re-mint API can issue a token under the new key if the current
   state's marked blob still exists. In the UI, click **Share inspect
   link** to Save for review again, subject to the normal gates.

There is no per-token revocation in v1 — rotation invalidates all.

### The payload store has reaped a blob a user needs

**Symptoms:** the reviewer's GET returns 404 with "ask the sender for
a fresh link."

**Procedure:**

1. The owner of the original session opens the composer at that
   session.
2. The owner performs **Save for review** again by clicking **Share inspect
   link**. Resolve any validation, completion or readiness blockers first.
   This records a new mark-time audit event and stores a new snapshot;
   re-minting alone cannot restore a deleted blob.
3. The owner sends the new URL to the reviewer.

The new snapshot describes the current composition and newly computed
readiness, not a recovery of the deleted historical snapshot. Send the URL
returned by this new mark; the audit trail records both mark events.

### The sessions DB has been deleted (data loss event)

Outstanding shareable links remain valid as long as:

* The signing key has not rotated.
* The payload store blob still exists.

However, the `composer_completion_events_table` rows are lost. An
operator cannot enumerate "who shared what" after a DB delete. If
auditable provenance matters, **back up the sessions DB before any
operator action that deletes it.**

## Troubleshooting

### Service refuses to start with "shareable_link_signing_key Field required"

The `web.shareable_link_signing_key` field is not set. Generate one
with `openssl rand -base64 32` and add it to the configuration.

### Service refuses to start with "shareable_link_signing_key must be at least 32 bytes"

The configured key is shorter than 32 bytes (base64-decoded). Regenerate
with `openssl rand -base64 32` and replace.

### Service refuses to start with "shareable_link_signing_key string inputs must be base64-encoded"

The configured value is a string that is not valid base64. String inputs
are base64-decoded at the boundary — never utf-8-encoded — so a
passphrase or a hex string is refused outright. Regenerate with
`openssl rand -base64 32` and replace.

### Service refuses to start with a `SESSION_SCHEMA_EPOCH` mismatch

The sessions DB predates the running code. Stop the candidate deployment and
preserve both stores. Under the temporary fresh-store-only policy, do not
delete, recreate, restamp, migrate or silently modify either store to satisfy
the candidate. Writable, read-only and inspection opens do not migrate any
predecessor Landscape epoch. Do not roll older code over a database initialized
by newer code. Diagnose the incompatibility with read-only inspection; a reset
or predecessor transition is outside the current policy.

### `POST /mark-ready-for-review` returns 409 with "No composition state exists for this session"

The session has never committed a composition state. `GET
/sessions/{session_id}/shareable-link` returns the same detail.

### `POST /mark-ready-for-review` returns 409 with "composition validation failed"

The composition has validation errors. Fix them in the composer and try
again.

### `POST /mark-ready-for-review` returns 409 with "interpretation review cards are waiting"

Validation halted only because LLM interpretation review cards are
unresolved; there are no errors to fix. Resolve the cards in the
composer and try again. The refusal is deliberate — a reviewer of an
un-reviewed composition would be attesting over unresolved cards.

### `POST /mark-ready-for-review` returns 409 with "composition completion gates have not passed"

Validation succeeded, but a completion gate — advisor sign-off, for
example — withheld `completion_ready`. Resolve the reported blockers and
try again. This is the axis the Save-for-review button is disabled on,
so the button normally forecloses this refusal.

### `POST /mark-ready-for-review` returns 409 with "readiness panel reports an error"

The audit-readiness panel has at least one row with `status == "error"`
— typically an unresolved validation issue, a missing secret reference,
or a plugin-trust gap. Resolve the error and try again. A row with
`status == "warning"` does not trip this gate. Pending LLM
interpretations are not an example of a non-blocking warning: they do
surface as a warning row, but the request is refused earlier, at the
validation gate, with "interpretation review cards are waiting".

### `POST /mark-ready-for-review` returns 409 with "composition changed while preparing the review snapshot"

Validation and readiness were computed against different composition
versions — the state moved while the snapshot was being prepared. Retry
against the current state.

### `POST /mark-ready-for-review` returns 409 with "Session operation is already active"

Marking ready is a writer: it takes COMPOSE authority on the session and
refuses while another compose operation holds the lease. Wait for the
in-flight operation to finish, then retry.

### `GET /sessions/{session_id}/shareable-link` returns 409

The re-mint route refuses when the current state has never been marked
ready ("mark this composition ready for review before requesting a
shareable link"), or when the mark-time audit row exists but its snapshot
blob is gone from the payload store ("current composition state has not
been marked ready for review"). Both require **Save for review** again
(the **Share inspect link** button), with its normal gates and audit write.

### `GET /sessions/shared/{token}` returns 401 unexpectedly

* The token has expired (`shareable_link_lifetime_seconds` past).
* The signing key has rotated since the token was minted.
* The token was truncated or tampered in transit (URL-decoding issues
  in email clients are a common cause).

Ask the sender to re-mint and resend.

### `GET /sessions/shared/{token}` returns 404

The snapshot is no longer in the payload store. Ask the sender to **Save for
review** again and send the newly returned URL. Re-minting cannot restore the
blob.

### `POST /mark-ready-for-review` returns 409 naming a file

The link was created with import allowed and a file the graph uses cannot
travel: it is missing or not ready, has no recorded hash, was written by the
assistant, or was produced by a pipeline run. Only files the user uploaded
travel. Re-attach the file, or create a view-only link. A 409 saying the file
"changed while preparing the share" or "does not match its recorded content"
means it was replaced while the link was being created; retry.

A file whose stored bytes no longer match their recorded hash, or whose bytes
are missing from storage, is not a 409: the request fails with 500 and no
link is created. That is storage corruption or loss, not something the user
can fix by retrying; investigate the blob store.

### `POST /mark-ready-for-review` returns 422 "a shared link can last at most …"

The requested lifetime is longer than `shareable_link_lifetime_seconds`.
The dialog offers only lifetimes the deployment accepts.

### `POST /sessions/shared/{token}/import` returns 403

Either the link is view-only ("This share link does not allow import"), or
the recipient's account lacks the deployment-wide `user` role
(`error_type: user_role_required`). The import page names which.

## Frontend surfaces (post-Phase-6B)

The user-facing entry points landed by Phase 6B:

* **Completion bar** — `components/composer/CompletionBar.tsx`. Mounted
  in the pipeline artifact action bar. Renders, in DOM order: Share inspect
  link (the Save-for-review operation), Import YAML (only when the per-user
  `show_advanced` preference is on), and Run pipeline. There is no Export YAML button —
  export lives on the YAML artifact tab's Copy/Download controls and on
  the command palette (Ctrl+Shift+Y). Run pipeline is not co-equal: it
  carries the danger-family red fill and is isolated at the bar's right
  edge, while Save and Import stay in the left cluster. The
  Save-for-review button is client-side-disabled when the backend-owned
  completion-readiness flag is not true — which includes the case where
  validation has not yet run — or while a mark request is in flight; its
  tooltip is the first completion blocker's detail. This mirrors the
  backend's `completion_not_ready` gate and is deliberately stricter
  than Run pipeline: a composition can be validation-valid and still
  have the button disabled.
* **Save-for-review dialog** —
  `components/composer/SaveForReviewDialog.tsx`. Mounted at app-root so
  the verb can open it from any focused view. It opens on a configure
  step — **Let recipients import a copy** (off by default, listing the
  files that would travel) and **Link expires after** (only the lifetimes
  the deployment accepts) — and mints nothing until **Create link**. Then
  three observable states: spinner (in-flight), error banner (with
  Try-again button), or success panel (share URL + copy-to-clipboard +
  Open-in-new-tab link + expiry timestamp). The share URL is the absolute form
  `${location.origin}/#/shared/{token}` — the backend returns the
  path-only suffix and the frontend prepends `location.origin`.
* **Shared inspect view** — `components/shared/SharedInspectView.tsx`.
  Mounted at app-root by a top-level branch in `App.tsx` when the URL
  hash matches `#/shared/{token}`. Renders read-only: pipeline
  metadata, the six-row audit-readiness panel served verbatim from the
  frozen blob, and the rendered YAML. The composer chat panel, run
  controls, and edit affordances are deliberately absent. On a link that
  allows import it shows the files that come with it and **Import a copy
  into my workspace**, which navigates to the new session on success and
  otherwise names the reason it was refused (for a plugin-policy refusal, the
  component and why its plugin is unavailable). After a refusal a retry
  cannot change (400, 403, 422) the button stays disabled until the page is
  reloaded.
* **Narrative results panel** —
  `components/composer/NarrativeResults.tsx`, conditionally rendered
  by `InlineRunResults` when `useNarrativeMode().narrativeMode` is
  true. Surfaces the plugin-emitted `summary` field; the tabular
  `RunOutputsPanel` remains visible alongside.

The hash-router extension (`useSharedToken` + `useHashRouter` guards)
ensures the shared-route URL is preserved across the entire shared-view
lifecycle. The session router will not mutate the hash while
`#/shared/*` is live; navigating away (via the "Return to my
workspace" link) returns control to the regular session router.

## References

* ADR: [docs/architecture/adr/022-shareable-reviews.md](../architecture/adr/022-shareable-reviews.md)
* Historical Phase 6A/6B implementation plans are preserved in git history or
  maintainer-local archives.
