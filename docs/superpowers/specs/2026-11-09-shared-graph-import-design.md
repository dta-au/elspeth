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

**Dialog.** Today opening the dialog mints the link immediately
(`shareableReviewStore.openAndMark`). It becomes two steps: **configure**, then
**create link**. The configure step reads the file list from a new
owner-only route, `GET /api/sessions/{session_id}/shareable-files`, which
returns the current state's referenced files (name, size, binding sites; no
blob ids or storage paths). The dialog gains:

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
- **Known cost — storage growth (decided 2026-11-09).** Nothing in the web app
  reclaims payload-store content: the only purge is the operator command
  `elspeth purge`, which deletes payloads referenced by Landscape runs.
  `WebSettings.payload_store_retention_days` drives nothing. Share snapshots
  already accumulate; frozen files make that growth larger. Because storage is
  content-addressed, two shares of the same file share one stored copy.
  Reclamation is tracked in dta-au/elspeth#356. No startup retention check is
  added: there is no setting that governs share content to check against.
- **Edge case:** `elspeth purge` reclaiming a run whose payload has bytes
  identical to a live share's frozen file deletes that file; import then returns
  the handled 404. Also tracked in dta-au/elspeth#356.
- **Re-minting keeps the chosen lifespan.** "Copy link again"
  (`get_shareable_link`) currently re-mints with the global maximum; it must
  instead reuse the original lifespan (`expires_at - created_at` from the
  `mark_ready_for_review` row), so a 1-day link is not stretched to 30 days.
- **Compatibility:** tokens minted before this release carry no import flag and
  are treated as view-only. Existing snapshot blobs (the old 7-key shape) remain
  readable as view-only; new snapshots use a 9-key shape adding `import_allowed`
  and `files`.

### 2. Importing (Chris)

**Shared view.** When the link allows import, `SharedInspectView` shows an
**"Import a copy into my workspace"** button and the list of files that come
with it. View-only and older links show no button; the server refuses them
regardless.

**Endpoint.** `POST /api/sessions/shared/{token}/import`, authenticated, strict
rate-limit bucket (as the inspect route). Library-fork parity (decided
2026-11-09): no staged session and no server-side idempotency, because both
would need a sessions-schema change. Order of operations:

1. **Verify the token and read everything first:** valid and unexpired (else
   401), `import_allowed` (else 403), snapshot and every frozen file present in
   the payload store with matching sha256 (else 404 "ask the sender for a fresh
   link"). Nothing is created until all reads succeed.
2. **Create Chris's session** (live), titled "Copy of ‹pipeline name›".
3. **Copy each frozen file** into it as an ordinary blob (`create_blob`), which
   enforces the session and user storage quotas. A quota refusal surfaces as the
   existing quota error.
4. **Seed the graph** from the snapshot's public projection through the ordinary
   YAML importer (the library-fork seeder), attaching every manifest binding to
   Chris's copy: sources via the existing `source_blob_ids`, nodes via a new
   `node_blob_bindings` that rebuilds each widened marker in full (`blob_ref`,
   `mode`, `sha256`, `encoding`) and checks the copy's hash against the
   manifest.
5. **Re-author sink output locations** for Chris's session, as YAML import does
   today; RAG `persist_directory` gets a fresh location.
6. **Record provenance** in the seeded state's `composer_meta` as
   `shared_import` (see §4). Respond with the new `session_id` and `state_id`.
7. **On any error after step 2**, archive the new session, as library fork does.

Chris lands in the new session with a banner naming Tom as the source. The
shared view lives outside the session router, so the frontend sets
`window.location.hash` to the new session after reloading the session list.
The Import button is disabled while its request is in flight.

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
| Share | Payload-store write fails part-way | No token; stray bytes remain (dta-au/elspeth#356) |
| Share | Lifespan above the server maximum | 422 |
| Import | Token tampered or expired | 401 |
| Import | Link is view-only | 403 |
| Import | Snapshot or a frozen file missing | 404, "ask the sender for a fresh link"; no session created |
| Import | Frozen file hash mismatch on read | Integrity error; no session created |
| Import | Files exceed Chris's quota | Existing quota error; the new session is archived |
| Import | Seeding fails | Error; the new session is archived |
| Import | Process dies part-way | **Accepted gap:** a visible, half-built "Copy of …" session can remain; Chris can delete it (same as library fork) |
| Import | Double click | Button disabled while in flight |
| Import | Deliberate retry after a network failure | **Accepted gap:** may create a second copy (no server-side idempotency) |

### 4. Audit

**No sessions-schema change** (decided 2026-11-09). The sessions database has
no migration pathway; a new column or event type would bump
`SESSION_SCHEMA_EPOCH` and force every deployment onto a fresh store.

- **Tom's share:** the existing `mark_ready_for_review` completion-event row
  already records `payload_digest` and `expires_at`. `import_allowed` and the
  file manifest live inside the content-addressed snapshot, so the row's digest
  binds them. Accepted gap: if the snapshot is ever reclaimed (dta-au/elspeth#356),
  the row still identifies what was shared but the manifest detail is gone.
- **Each import:** recorded as `shared_import` provenance in the `composer_meta`
  of Chris's seeded composition state (an immutable, audited row — the
  mechanism `library_fork` uses): snapshot digest, Tom's user id and username,
  Tom's session and state ids, and each file's sha256 with its new blob id.
  "Who imported this share" is answered by a server-side query on the digest.
  A chat `audit` message is not used: its `writer_principal` vocabulary is a
  closed CHECK with no import value. Accepted gap: nothing is written on Tom's
  side, and there is no user-facing notification.

### 5. Composer invariants

Import copies a graph Tom authored, exactly as library fork copies a published
one. The server authors no pipeline structure and nothing is tutorial-specific,
so both non-negotiable composer invariants hold.

## Testing

1. **Unit:** manifest construction (source `blob_ref`, `blob_rows` source
   `options.blobs[].blob_id`, and nested node markers); token flag and expiry
   cap; re-mint preserving the chosen lifespan; old 7-key snapshots reading as
   view-only; `node_blob_bindings` in the importer; all-reads-before-create;
   archive-on-failure. Each test is watched failing before the implementation.
2. **Integration:** Tom shares a graph with a source file and a `reference_join`
   inline file; Chris imports; files arrive with identical hashes and every
   binding points at Chris's copy. Refusal paths: view-only, expired, tampered,
   missing frozen file (no session created), over quota and seeding failure
   (session archived).
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
