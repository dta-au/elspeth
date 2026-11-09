# Shared graph import — design

**Status:** design approved in conversation 2026-11-09; awaiting written-spec review.
**Target release:** 0.8.10.
**Branch:** `claude/brave-albattani-yjqujo` (built on `claude/vibrant-euler-tora25`).

## Goal

Tom shares a workflow with Chris through a link. Chris opens the link, sees the
existing read-only share view, and clicks **Import** to get a new session of his
own containing Tom's workflow, including the uploaded files the workflow reads.

Success means:

- Chris's new session holds Tom's graph structure and its own copies of every
  uploaded file the graph is attached to, with each attachment pointing at
  Chris's copy.
- Tom decides, per link, whether recipients may import and sees exactly which
  files leave his account before confirming.
- Tom's and Chris's copies are independent afterwards; neither sees the other's
  later edits.
- Secrets never travel.

## Decisions taken

| Question | Decision |
|---|---|
| What "import" means | Create a **new session** owned by the recipient (not replace or merge into an existing one). |
| Who may import | **Tom opts in per link.** Links minted before this feature, and links without the opt-in, stay view-only. |
| Do files travel | **Yes.** Every uploaded file the graph is attached to travels, wherever the attachment is (source `blob_ref`, transform inline-content markers). |
| Which files | **Only files the shared graph references**, not every file in Tom's session. |
| When files are captured | **Frozen at share time** into storage that belongs to the share, so later edits, replacement, deletion or archiving on Tom's side cannot break a valid link. |
| Link lifespan | **Tom picks per link** (1, 7 or 30 days); the existing `shareable_link_lifetime_seconds` setting becomes the maximum. |
| Early revocation | **Out of scope.** Tom chooses a short lifespan instead. |
| RAG vector indexes (`persist_directory`) | **Not copied.** Chris's copy gets a fresh, empty index location and rebuilds it. |
| Sink output locations | **Not copied.** Re-authored for Chris's session, as YAML import already does. |
| Notify Tom on import | **No user-facing notification.** The import is audited on Chris's session, keyed by the share's snapshot digest. |
| Sessions schema | **No change** (no migration pathway; an epoch bump would force a fresh store). |

## What already exists

- **Share links** (`src/elspeth/web/shareable_reviews/`): "Save for review" mints a
  signed, expiring capability token over a content-addressed snapshot in the
  `FilesystemPayloadStore`. The recipient must be authenticated and sees a
  read-only `SharedInspectView` at `/#/shared/<token>`. The snapshot stores the
  **public, custody-redacted** projection of the graph.
- **Library fork** (`POST /api/library/{entry_id}/fork`,
  `src/elspeth/web/sessions/routes/workflow/library.py`): imports a stored YAML
  into a new session through the injected `library_state_seeder`, refuses
  credential material, records `library_fork` provenance, and archives the new
  session if seeding fails.
- **Same-user session fork blob copy** (`BlobService.copy_blobs_for_fork`,
  `src/elspeth/web/blobs/service.py`): copies a frozen, hash-and-size-pinned blob
  plan into a staged, archived child session under quota and write fences. It
  refuses unless source and target belong to the same principal
  (`_verify_fork_child_custody`).
- **Source re-binding on import**: `ImportStateYamlRequest.source_blob_ids`
  attaches uploaded blobs to sources. There is no equivalent for transforms.
- **Projection fix (landed first, `d9ad5ba7`)**: the public projection now
  removes transform file markers whole and reports them per node; the importer
  refuses headless marker fragments. This feature depends on it.

## Design

### 1. Sharing (Tom)

**Dialog.** The "Save for review" dialog gains:

1. **"Let recipients import a copy"**, off by default. When ticked it lists the
   files that will travel: name, size, and which component and option uses each.
2. **"Link expires after"**: 1 day, 7 days, 30 days. The server refuses any value
   above `shareable_link_lifetime_seconds` (422); the browser is not trusted to
   enforce the cap.

**Server, on confirm:**

1. Re-validate the composition exactly as today (validation and readiness gates).
2. If import is allowed, collect every file attachment in the current graph:
   source `blob_ref` bindings and widened `inline_content` markers at any depth in
   node options. For each, require the blob to be `ready` and its stored
   `content_hash` and size to match; otherwise refuse with 409 naming the file.
3. Copy each referenced file's bytes into the payload store (content-addressed).
4. Write the snapshot with two new fields:
   - `import_allowed: bool`
   - `files: [...]`, one entry per file: display filename, MIME type, sha256,
     size, and every binding site — `{"component": "source", "name": ...,
     "option": "blob_ref"}` or `{"component": "node", "id": ..., "option_path":
     "tables.t.reference_content", "mode": "inline_content", "encoding": ...}`.
5. Sign a token whose payload covers the snapshot digest, the chosen expiry, and
   `import_allowed`.
6. Write the existing "ready for review" audit row unchanged (it records the
   snapshot digest and expiry; the digest binds `import_allowed` and the
   manifest — see §4).

The `files` manifest is private to the snapshot: the shared inspect response
continues to expose only the public projection plus a recipient-safe file list
(names and sizes), never blob ids or storage paths.

**Expiry and storage.**

- An expired link stops working for both inspect and import immediately, through
  the existing token verification.
- Frozen bytes are reclaimed by the payload store's normal retention sweep.
  Because storage is content-addressed, two shares of the same file share one
  stored copy; a link never deletes "its" bytes directly.
- **Constraint:** the retention that actually governs payload-store reaping must
  be at least the maximum link lifetime, or a valid link can lose its files. The
  server refuses to start when it is not. Note: `WebSettings.payload_store_retention_days`
  is documented as informational only; the implementation plan must identify the
  setting that drives the reaper and check that one. Defaults today: 90 days
  retention, 30 days maximum lifetime.
- **Compatibility:** tokens minted before this release carry no import flag and
  are treated as view-only. Existing snapshot blobs remain readable.

### 2. Importing (Chris)

**Shared view.** When the link allows import, `SharedInspectView` shows an
**"Import a copy into my workspace"** button and the list of files that come
with it. View-only and older links show no button; the server refuses them
regardless.

**Endpoint.** `POST /api/sessions/shared/{token}/import`, authenticated, strict
rate-limit bucket (as the inspect route), with a client-supplied idempotency key
per click. Order of operations:

1. **Verify the token:** valid and unexpired (else 401), `import_allowed` (else
   403), snapshot and files present in the payload store (else 404 "ask the
   sender for a fresh link").
2. **Check Chris's storage quota** against the total file size before creating
   anything; refuse clearly if it does not fit.
3. **Create a staged session** for Chris, archived and invisible, titled
   "Copy of ‹pipeline name›".
4. **Copy each frozen file** from the payload store into Chris's staged session as
   an ordinary blob, re-verifying sha256 on read and assigning deterministic
   target blob ids so a retry does not duplicate files.
5. **Seed the graph** from the snapshot's public projection through the ordinary
   YAML importer, attaching every manifest binding to Chris's copy: sources via
   the existing `source_blob_ids`, nodes via a new `node_blob_bindings` that
   rebuilds each widened marker in full (`blob_ref`, `mode`, `sha256`,
   `encoding`) and checks the copy's hash against the manifest.
6. **Re-author sink output locations** for Chris's session, as YAML import does
   today; RAG `persist_directory` gets a fresh location.
7. **Record provenance** on Chris's session as `shared_import`: snapshot digest,
   Tom's user id and username, Tom's session and state ids, and the file hashes.
8. **Publish the session** (un-archive) in one step and write the import audit
   event. Respond with the new `session_id` and `state_id`.

Chris lands in the new session with a banner naming Tom as the source.

**Secrets.** The existing credential check
(`require_no_credential_material_in_state`) refuses any secret value. Secret
*references* arrive unresolved; Chris's readiness panel shows them as missing
until he adds his own.

**Graphs that do not validate for Chris** (missing secrets, a plugin his policy
disallows) still import; the readiness panel reports the problems, as with a
library fork.

### 3. Failure handling

| When | Situation | Result |
|---|---|---|
| Share | Referenced file missing, not ready, or hash/size mismatch | 409 naming the file; no token |
| Share | Payload-store write fails part-way | No token; stray bytes are reaped |
| Share | Lifespan above the server maximum | 422 |
| Import | Token tampered or expired | 401 |
| Import | Link is view-only | 403 |
| Import | Snapshot or files reaped | 404, "ask the sender for a fresh link" |
| Import | Files exceed Chris's quota | Refused before any session exists |
| Import | Frozen file hash mismatch on read | Integrity error; staged session and copies cleaned up |
| Import | Process dies part-way | Only an archived staged session remains; existing fork cleanup handles it |
| Import | Double click / retry with the same idempotency key | Returns the same session |

The staged-publish sequence replaces the library fork's create-live-then-archive
pattern for this endpoint so that a crash cannot leave a visible empty session.

### 4. Audit

**No sessions-schema change** (decided 2026-11-09). The sessions database has
no migration pathway; a new column or event type would bump
`SESSION_SCHEMA_EPOCH` and force every deployment onto a fresh store.

- **Tom's share:** the existing `mark_ready_for_review` completion-event row
  already records `payload_digest` and `expires_at`. `import_allowed` and the
  file manifest live inside the content-addressed snapshot, so the row's digest
  binds them. Accepted gap: once payload retention reaps the snapshot, the row
  still identifies what was shared but the manifest detail is gone.
- **Each import:** recorded on **Chris's** session as an `audit`-role session
  message (the mechanism the session fork uses for its blob plan) carrying the
  snapshot digest, Tom's user id, Tom's session and state ids, the file hashes,
  and the new session id. "Who imported this share" is answered by a server-side
  query on the digest. Accepted gap: nothing is written on Tom's side, and there
  is no user-facing notification.
- **Chris's session:** `shared_import` provenance in composer metadata.

### 5. Composer invariants

Import copies a graph Tom authored, exactly as library fork copies a published
one. The server authors no pipeline structure and nothing is tutorial-specific,
so both non-negotiable composer invariants hold.

## Testing

1. **Unit:** manifest construction (source and node bindings, nested and list
   positions); token flag and expiry cap; retention-versus-lifetime startup
   check; `node_blob_bindings` in the importer; quota pre-check; staged-publish
   failure paths. Each test is watched failing before the implementation.
2. **Integration:** Tom shares a graph with a source file and a `reference_join`
   inline file; Chris imports; files arrive with identical hashes and every
   binding points at Chris's copy. Refusal paths: view-only, expired, tampered,
   reaped, over quota, simulated crash mid-import.
3. **PostgreSQL:** this touches session and blob persistence, so
   `pytest tests/ -m testcontainer -n 0` must run (Docker required).
4. **Frontend:** component tests for the dialog (checkbox, file list, expiry
   picker) and the Import button with its error states; one Playwright test for
   the Tom → Chris flow.
5. **Whole-tree gates and the full suite** before merge.

## Out of scope

- Early link revocation.
- Importing into, replacing, or merging with an existing session.
- Copying RAG vector indexes or past run outputs.
- Node re-binding for ordinary pasted-YAML import (the `node_blob_bindings`
  mechanism makes this a small follow-up, but it is not part of this feature).
- Changing the same-user session fork, which keeps copying every ready blob.
