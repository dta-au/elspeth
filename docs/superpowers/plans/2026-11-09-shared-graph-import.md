# Shared Graph Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A share link can let its recipient import a copy of the shared graph, with every uploaded file the graph is attached to, into a new session of their own.

**Architecture:** At share time the owner's referenced files are frozen into the content-addressed payload store and listed in a manifest inside the (digest-bound) share snapshot. A new import route verifies the token, reads every frozen file, creates the recipient's session, copies the files in as ordinary blobs, and seeds the graph through the existing YAML importer with new re-binding inputs for inline markers and multi-file sources — library-fork parity, no sessions-schema change.

**Tech Stack:** Python 3.12+/FastAPI/pydantic/SQLAlchemy (backend), React + TypeScript + Vitest + Playwright (frontend).

**Spec:** `docs/superpowers/specs/2026-11-09-shared-graph-import-design.md`

## Global Constraints

- **No sessions-schema change.** Do not touch `src/elspeth/web/sessions/models.py` tables, CHECK vocabularies, or `SESSION_SCHEMA_EPOCH` (78).
- No staged/archived sessions and no server-side idempotency for import (library-fork parity). On any failure after the session exists: `session_service.archive_session(session.id)`.
- Lifespans offered: exactly `86400`, `604800`, `2592000` seconds (1, 7, 30 days). A requested lifespan above `WebSettings.shareable_link_lifetime_seconds` → 422.
- Snapshot shapes: legacy 7 keys (read-only, view-only) and new 9 keys (`import_allowed`, `files` added). `_parse_blob` accepts exactly one of the two closed sets; never a subset/superset test.
- Recipient-facing responses never contain blob ids, storage paths or sha256 values of the sharer's files — names and sizes only.
- Read CONTRIBUTING.md § Whole-tree gates before writing code. No new `getattr`/`hasattr`; no `# noqa`/`# type: ignore` (the existing `# noqa: B008` on FastAPI `Depends`/`Body` defaults is house convention and may be copied). Re-pin the soft-mapping census with `python -m scripts.check_contracts --write-census` in the commit that moves a site.
- Any new walker over planner-authored option values carries `@observation_boundary(tier=3, …)` like `_recursive_public_option_projection` (`src/elspeth/web/composer/yaml_generator.py`), with a `test_ref` when it raises.
- Composer invariants: import copies an owner-authored graph; the server authors no pipeline structure; no tutorial-specific code.
- Run focused tests with `-n 0`; run tests to a log file and read the exit code (AGENTS.md § Test Verification Policy).
- Changelog entries go under `## Unreleased (0.8.10)` in `CHANGELOG.md`.

## Review Focus

1. **An LLM-authored file in the shared graph.** Copying it with `create_blob` would turn it into a user-verbatim blob in the recipient's account, laundering it onto LLM prompt surfaces that refuse LLM-authored blobs. Expected: share-with-import refuses (409) naming the file; view-only sharing still works. Test owned by Task 3.
2. **One file bound in two places** (a source and a `reference_join` table, or two nodes). Expected: stored once, copied once for the recipient, both bindings point at the same new blob. Tests owned by Tasks 2 and 7.
3. **The sharer deletes the file or archives the session after sharing.** Expected: import still succeeds from the frozen copy. Test owned by Task 7.
4. **The link expires between Chris opening it and clicking Import.** Expected: an "expired link" message, and Chris stays signed in (a capability 401 must not log out). Test owned by Task 10.
5. **The sharer imports their own link.** Expected: works; a second, independent session. Test owned by Task 7.

---

### Task 1: Public projection removes `blob_rows` file entries

The public projection keeps each `blob_rows` entry minus `blob_id`, leaking `payload_ref` (the file's sha256), filename, MIME type and size, and reports nothing (measured 2026-11-09). Same defect class as `d9ad5ba7`.

**Files:**
- Modify: `src/elspeth/web/composer/yaml_generator.py` (`_recursive_public_option_projection`, `public_export_redaction`, `_MARKER_LABELS`, the source linkage constant)
- Test: `tests/unit/web/composer/test_yaml_generator.py`

**Interfaces:**
- Produces: a public composition whose `blob_rows` sources carry no `blobs` key; `public_export_redaction(state)["sources"][name]` includes `"blobs"` when it was removed; header label for `blobs` is `blob-linkage`.

- [ ] **Step 1: Write the failing tests** in a new class `TestPublicProjectionRemovesBlobRowsEntries`:
  - `test_blob_rows_entries_are_removed_whole`: a `blob_rows` source with `options={"blobs": [{"blob_id": B, "payload_ref": "ef"*32, "filename": "a.csv", "mime_type": "text/csv", "size_bytes": 10}]}` → public source options `== {}`.
  - `test_no_payload_ref_reaches_public_yaml`: `"ef"*32` and `"a.csv"` not in `generate_public_yaml(state)`.
  - `test_redaction_reports_blobs_for_the_source`: `public_export_redaction(state)["sources"] == {"source": ["blobs"]}`.
  - `test_marker_labels_cover_every_stripped_key` (existing) keeps passing once `blobs` joins the linkage set.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/composer/test_yaml_generator.py -n 0 -k "BlobRows or marker_labels"` — expect the three new tests FAIL.
- [ ] **Step 3: Implement.** Add `"blobs"` to the source-level linkage set (`_PUBLIC_SOURCE_LINKAGE_KEYS`) and strip it at source top level in the projection (the `strip_bind_source_mode=True` call is the source-only call). Add `"blobs": "blob-linkage"` to `_MARKER_LABELS`. `sources_reading_uploaded_blobs` then also names `blob_rows` sources — intended.
- [ ] **Step 4: Run** the yaml generator, yaml importer and `tests/unit/web/sessions/test_routes_state.py` files — all PASS.
- [ ] **Step 5: Commit** `fix(composer): remove blob_rows file entries from the public projection` and add one bullet under the existing `## Unreleased (0.8.10)` › `### Composer` changelog entry.

### Task 2: File-binding collector

**Files:**
- Create: `src/elspeth/web/shareable_reviews/file_manifest.py`
- Test: `tests/unit/web/shareable_reviews/test_file_manifest.py`

**Interfaces:**
- Produces:
  ```python
  BindingKind = Literal["source_blob_ref", "source_blob_rows", "inline_content"]

  @dataclass(frozen=True, slots=True)
  class FileBinding:
      kind: BindingKind
      source_name: str | None      # set for source_* kinds
      row_index: int | None        # set for source_blob_rows
      field_path: str | None       # set for inline_content; canonical form
                                   # "source:<name>.options.a" / "node:<id>.options.a.b" / "output:<name>.options.a"
      encoding: ContentEncoding | None  # inline_content only; "utf-8" when the marker omits it

  @dataclass(frozen=True, slots=True)
  class BoundBlob:
      blob_id: UUID
      binding: FileBinding

  class ShareManifestError(Exception):
      reason: Literal["marker_in_list", "malformed_binding"]
      detail: str

  def collect_file_bindings(state: CompositionState) -> tuple[BoundBlob, ...]
  def file_binding_to_json(binding: FileBinding) -> dict[str, object]
  def file_binding_from_json(value: object) -> FileBinding   # raises ValueError on any shape drift
  ```
  Result ordering: sources (by name), then nodes (state order), then outputs (state order); within a component, option keys in sorted order. Validate every `field_path` by constructing it through the same pattern `BlobInlineRef` enforces (`contracts/blobs_inline.py` `_FIELD_PATH_PATTERN`).

- [ ] **Step 1: Write failing tests:**
  - `test_collects_source_blob_ref` → one `BoundBlob(B, FileBinding("source_blob_ref", "source", None, None, None))`.
  - `test_collects_blob_rows_entries_with_index` → two entries, `row_index` 0 and 1.
  - `test_collects_nested_inline_marker_with_canonical_field_path` → `field_path == "node:lookup.options.tables.t.reference_content"`, `encoding == "utf-8"` when omitted, `"latin-1"` when present.
  - `test_collects_inline_markers_on_sources_and_outputs` → `source:source.options.x`, `output:out.options.y`.
  - `test_same_blob_in_two_places_yields_two_bindings` (Review Focus 2).
  - `test_marker_inside_a_list_raises_marker_in_list`.
  - `test_bind_source_widened_marker_is_not_an_inline_binding` (a nested `mode: bind_source` marker raises `malformed_binding` — only top-level `blob_ref` binds a source).
  - `test_binding_json_round_trips` and `test_binding_from_json_rejects_extra_and_missing_keys`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/shareable_reviews/test_file_manifest.py -n 0` — FAIL (module missing).
- [ ] **Step 3: Implement** using `is_widened_blob_ref` for nested values; top-level source `blob_ref` (string UUID) and `blobs[i]["blob_id"]` for `blob_rows`. The option walker carries `@observation_boundary(tier=3, …)`; `test_ref` points at `test_marker_inside_a_list_raises_marker_in_list`.
- [ ] **Step 4: Run** the test file — PASS. Run `.venv/bin/python -m scripts.check_contracts` and the attribute/masquerade gates (`tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/elspeth_lints/test_masquerade_gate.py`) — PASS (re-pin the census if it moved).
- [ ] **Step 5: Commit** `feat(share): collect every file binding in a composition`.

### Task 3: Snapshot v2, import flag, per-link lifespan, frozen files

**Files:**
- Modify: `src/elspeth/web/shareable_reviews/service.py` (`_BlobShape`, `_BLOB_KEYS`, `_build_snapshot`, `mark_ready_for_review`, `get_shareable_link`, `_parse_blob`, `__init__`)
- Modify: `src/elspeth/web/shareable_reviews/models.py` (new `MarkReadyForReviewRequest`)
- Modify: `src/elspeth/web/shareable_reviews/routes.py` (mark-ready body)
- Modify: `src/elspeth/web/app.py` (pass `blob_service` to `ShareableReviewService`)
- Test: `tests/unit/web/shareable_reviews/test_service.py`, `tests/integration/web/test_shareable_reviews_routes.py`

**Interfaces:**
- Consumes: `collect_file_bindings`, `file_binding_to_json` (Task 2).
- Produces:
  ```python
  class MarkReadyForReviewRequest(_StrictResponse):   # strict, extra="forbid"
      allow_import: bool = False
      lifetime_seconds: Literal[86400, 604800, 2592000] | None = None   # None → settings maximum

  # route: body: MarkReadyForReviewRequest | None = Body(default=None)  # noqa: B008  (absent body = today's behaviour)

  async def mark_ready_for_review(self, *, session_id: UUID, user_id: str, username: str,
      session_operation_context: SessionOperationContext,
      allow_import: bool = False, lifetime_seconds: int | None = None) -> MarkReadyForReviewResponse

  ShareFileEntry = TypedDict: {"sha256": str, "size_bytes": int, "filename": str,
                               "mime_type": str, "bindings": list[dict[str, object]]}
  # snapshot gains "import_allowed": bool and "files": list[ShareFileEntry] (sorted by sha256; [] when not importable)

  @dataclass(frozen=True, slots=True)
  class ParsedSnapshot:   # what _parse_blob now returns
      blob: _BlobShape            # the 7 legacy keys
      import_allowed: bool
      files: tuple[ShareFileEntry, ...]
  ```
  ```python
  # public loader for the import path (Task 7); verify + retrieve + parse, same errors as resolve_token
  async def load_snapshot(self, *, token: str) -> tuple[ShareTokenPayload, ParsedSnapshot]
  ```
  `ShareableReviewService.__init__` gains `blob_service: BlobServiceProtocol`.
  New `CompositionNotRunnableError` reasons: `"file_not_shareable"` (409, detail names the file), `"lifetime_exceeds_maximum"` (mapped to 422 by the route).

- [ ] **Step 1: Write failing tests:**
  - `test_mark_ready_without_import_writes_nine_key_snapshot_with_empty_files`.
  - `test_mark_ready_with_import_freezes_each_referenced_file_once` — two bindings of one blob → one `files` entry with two `bindings`; `payload_store.exists(sha256)`; the entry's sha256 equals the blob row's `content_hash`.
  - `test_mark_ready_with_import_refuses_llm_authored_file` → `CompositionNotRunnableError(reason="file_not_shareable")` naming the filename; no audit row, nothing stored (Review Focus 1).
  - `test_mark_ready_with_import_refuses_not_ready_or_hash_mismatched_file`.
  - `test_mark_ready_with_import_refuses_marker_in_list` (Task 2 error → `file_not_shareable`).
  - `test_mark_ready_lifetime_sets_expiry` (1 day → `expires_at == created_at + 86400s`) and `test_mark_ready_lifetime_above_maximum_is_refused` (settings max 604800, request 2592000).
  - `test_get_shareable_link_keeps_the_original_lifespan` — mark with 1 day; re-mint → `expires_at - now ≈ 86400s`, not the global maximum.
  - `test_parse_blob_reads_legacy_seven_key_snapshot_as_view_only`; keep `test_resolve_token_rejects_obsolete_signed_blob_shape` for partial shapes; add `test_parse_blob_rejects_non_bool_import_allowed_and_malformed_files`.
  - Route: `test_mark_ready_accepts_absent_body` (unchanged behaviour) and `test_mark_ready_rejects_unknown_lifetime_with_422`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/shareable_reviews -n 0` and `tests/integration/web/test_shareable_reviews_routes.py -n 0` — new tests FAIL.
- [ ] **Step 3: Implement.** In `mark_ready_for_review`, after the readiness gates and before `_build_snapshot`: when `allow_import`, collect bindings, then per distinct blob `get_blob` + `read_blob_content` under the existing COMPOSE context; refuse if `status != "ready"`, `content_hash is None`, `creation_modality` is not verbatim, or `sha256(bytes) != content_hash`; `payload_store.store(bytes)` must return `content_hash`. Store files **before** the audit row (no token is minted unless every file is frozen). `get_shareable_link` computes `lifetime = event.expires_at - event.created_at`. Wire `blob_service=app.state.blob_service` in `app.py`.
- [ ] **Step 4: Run** both test paths from Step 2 — PASS.
- [ ] **Step 5: Commit** `feat(share): per-link lifespan and import opt-in with frozen files`.

### Task 4: Owner's file preview route

**Files:**
- Modify: `src/elspeth/web/shareable_reviews/service.py`, `models.py`, `routes.py`
- Test: `tests/unit/web/shareable_reviews/test_service.py`, `tests/integration/web/test_shareable_reviews_routes.py`, `tests/unit/web/auth/test_pipeline_user_authorization.py` (route inventory)

**Interfaces:**
- Produces:
  ```python
  class ShareableFileView(_StrictResponse):
      filename: str
      size_bytes: int
      used_by: list[str]        # e.g. "source 'customers'", "node 'lookup' (tables.t.reference_content)"
  class ShareableFilesResponse(_StrictResponse):
      files: list[ShareableFileView]
      importable: bool
      not_importable_reason: str | None   # e.g. "'notes.txt' was written by the assistant and cannot be shared for import"

  GET /api/sessions/{session_id}/shareable-files   # require_pipeline_user + verify_session_ownership; read bucket
  async def preview_shareable_files(self, *, session_id: UUID, session_operation_context: SessionOperationContext) -> ShareableFilesResponse
  ```
  Same refusal rules as Task 3, reported instead of raised. No blob ids, paths or hashes in the response.

- [ ] **Step 1: Write failing tests:** `test_preview_lists_each_file_once_with_every_use`, `test_preview_reports_llm_authored_file_as_not_importable`, `test_preview_route_is_owner_only_404_for_other_user`, `test_preview_response_contains_no_blob_identity` (blob id, storage path and `content_hash` absent from the JSON).
- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement**, sharing the per-blob checks with Task 3 through one private helper (`_check_shareable_blob(record, content) -> str | None` returning a refusal message).
- [ ] **Step 4: Run** — PASS, including the route-auth inventory test.
- [ ] **Step 5: Commit** `feat(share): preview the files a share would carry`.

### Task 5: Recipient-safe file list on the shared inspect response

**Files:**
- Modify: `src/elspeth/web/shareable_reviews/models.py` (`SharedInspectResponse`), `service.py` (`resolve_token`)
- Test: `tests/unit/web/shareable_reviews/test_models.py`, `test_service.py`

**Interfaces:**
- Produces: `SharedInspectResponse` gains `import_allowed: bool` and `files: list[SharedFileSummary]`, where `SharedFileSummary(_StrictResponse)` has `filename: str`, `size_bytes: int`.

- [ ] **Step 1: Write failing tests:** `test_resolve_token_exposes_import_flag_and_file_names_only` (no sha256 in the dumped JSON), `test_resolve_token_legacy_snapshot_is_view_only_with_no_files`, and update the strict-model pins in `test_models.py` for the two new required fields.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the projection in `resolve_token` from `ParsedSnapshot`.
- [ ] **Step 4: Run** `tests/unit/web/shareable_reviews -n 0` — PASS.
- [ ] **Step 5: Commit** `feat(share): show recipients whether a link imports and which files come with it`.

### Task 6: Importer re-binds inline markers and multi-file sources

**Files:**
- Modify: `src/elspeth/web/sessions/routes/composer/state.py` (`ImportStateYamlRequest`, `seed_state_from_runtime_yaml`, new helpers beside `_state_with_imported_source_blobs`, meta TypedDicts)
- Modify: `src/elspeth/web/sessions/routes/workflow/library.py` (`LibraryStateSeeder` meta type)
- Test: `tests/unit/web/sessions/test_routes_state.py`

**Interfaces:**
- Produces:
  ```python
  class InlineBlobBindingRequest(BaseModel):   # extra="forbid"
      blob_id: str                              # canonical UUID
      encoding: ContentEncoding = "utf-8"

  class SourceBlobRowsBindingRequest(BaseModel):  # extra="forbid"
      blob_ids: list[str]                          # in row order, ≥1

  class ImportStateYamlRequest:   # existing; gains
      inline_blob_bindings: dict[str, InlineBlobBindingRequest] | None = None   # key: canonical field_path
      source_blob_rows: dict[str, SourceBlobRowsBindingRequest] | None = None   # key: source name

  class SharedImportFile(TypedDict):  sha256: str; blob_id: str
  class SharedImportFacts(TypedDict):
      payload_digest: str; shared_by_user_id: str; shared_by_username: str
      shared_from_session_id: str; shared_from_state_id: str; files: list[SharedImportFile]
  class SharedImportMetaUpdates(TypedDict):  shared_import: SharedImportFacts
  ComposerSeedMetaUpdates = LibraryForkMetaUpdates | SharedImportMetaUpdates
  # seed_state_from_runtime_yaml(..., composer_meta_updates: ComposerSeedMetaUpdates | None = None)
  ```
  This is the spec's `node_blob_bindings`, generalised to any component because inline markers may sit on sources and outputs too.
  Inline rebuild: marker `{"blob_ref": str(blob.id), "mode": "inline_content", "sha256": blob.content_hash}` plus `"encoding"` only when not `"utf-8"` (the canonical writer's shape, `composer/tools/blobs.py` `_execute_wire_blob_inline_ref`), set at the field path, creating missing intermediate mappings; refuse (400) if a value already sits there. Rows rebuild: `options["blobs"] = [{"blob_id", "payload_ref": content_hash, "filename", "mime_type", "size_bytes"}, …]` from Chris's blob records (the `_resolve_source_blobs` shape). Every referenced blob must be in this session and `ready` (404/409 as the existing source helper does).

- [ ] **Step 1: Write failing tests:** `test_import_rebinds_inline_marker_at_field_path`, `test_import_inline_binding_keeps_non_utf8_encoding`, `test_import_inline_binding_refuses_occupied_path`, `test_import_inline_binding_refuses_foreign_or_unready_blob`, `test_import_rebinds_blob_rows_source_in_order`, `test_import_rejects_malformed_field_path_with_400`, and `test_seed_records_shared_import_meta`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/sessions/test_routes_state.py -n 0 -k "rebind or shared_import or field_path"` — FAIL.
- [ ] **Step 3: Implement** `_state_with_imported_inline_blobs(...)` and `_state_with_imported_source_blob_rows(...)` (same signature shape as `_state_with_imported_source_blobs`), called right after it in `seed_state_from_runtime_yaml`.
- [ ] **Step 4: Run** the whole `test_routes_state.py` and `tests/unit/web/composer/test_yaml_importer.py` — PASS; run `scripts.check_contracts`.
- [ ] **Step 5: Commit** `feat(composer): YAML import re-binds inline file markers and multi-file sources`.

### Task 7: Import route

**Files:**
- Create: `src/elspeth/web/shareable_reviews/importer.py`
- Modify: `src/elspeth/web/shareable_reviews/routes.py`, `models.py`
- Test: `tests/unit/web/shareable_reviews/test_importer.py`, `tests/integration/web/test_shareable_reviews_routes.py` (incl. the rate-bucket pin at `test_review_write_endpoints_use_write_bucket_and_resolve_stays_strict`), `tests/unit/web/auth/test_pipeline_user_authorization.py`

**Interfaces:**
- Consumes: `ShareableReviewService.load_snapshot` and `ParsedSnapshot` (Task 3), `file_binding_from_json` (Task 2), the Task 6 request fields and `SharedImportMetaUpdates`.
- Produces:
  ```python
  class SharedImportResponse(_StrictResponse):  session_id: UUID; state_id: str
  class ImportNotAllowedError(Exception): ...

  async def import_shared_snapshot(*, token: str, request: Request, user: UserIdentity) -> SharedImportResponse

  POST /api/sessions/shared/{token}/import   # require_pipeline_user; strict bucket (get_rate_limiter); 201
  ```
  Order: verify + parse (InvalidToken → 401 "Invalid or expired share token"); `import_allowed` (else `ImportNotAllowedError` → 403); `payload_store.retrieve` every file (`PayloadNotFoundError` → 404 with the inspect route's message; `IntegrityError` → 500) — **all before creating anything**; resolve `app.state.library_state_seeder` (503 if unwired, as library fork); `create_session(user.user_id, title, provider)` with title `f"Copy of {name}"` or `"Copy of shared pipeline"` when the name is blank; acquire a `SessionOperationKind.CREATE` lease and `create_blob(session.id, filename, content, mime_type, created_by="user", source_description=f"imported from a shared link ({payload_digest})")` once per distinct sha256; close the lease; seed with `yaml=` the snapshot's public YAML, `source_blob_ids`, `source_blob_rows`, `inline_blob_bindings`, and `composer_meta_updates={"shared_import": …}`. Any exception after `create_session` → `archive_session(session.id)` then re-raise (library-fork pattern, preserving the original exception if archive also fails). Quota errors map exactly as `create_blob_upload` maps them (`blobs/routes.py`).

- [ ] **Step 1: Write failing tests** (`test_importer.py`, real services where the existing shareable-review tests use them):
  - `test_import_copies_files_and_rebinds_every_binding` — source `blob_ref` + `reference_join` marker + `blob_rows`; new blobs have identical `content_hash`; every binding points at the recipient's blob ids; meta `shared_import.files` lists them; no option anywhere in the new state contains the sharer's session id, blob ids or storage paths (sink paths and RAG `persist_directory` come from the importer's own defaults).
  - `test_import_one_file_bound_twice_creates_one_blob` (Review Focus 2).
  - `test_import_after_sharer_deletes_file_still_succeeds` (Review Focus 3).
  - `test_sharer_can_import_own_link` (Review Focus 5).
  - `test_import_view_only_link_is_403_and_creates_no_session`.
  - `test_import_missing_frozen_file_is_404_and_creates_no_session`.
  - `test_import_over_quota_archives_the_new_session`.
  - `test_import_seed_failure_archives_the_new_session` (e.g. a plugin the recipient's policy disallows → 422 from the seeder).
  - `test_import_blank_pipeline_name_uses_fallback_title`.
  - Route: `test_import_route_requires_authentication`, rate-bucket pin updated, auth inventory updated.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/shareable_reviews/test_importer.py -n 0` — FAIL.
- [ ] **Step 3: Implement** `importer.py` and the route.
- [ ] **Step 4: Run** `tests/unit/web/shareable_reviews`, `tests/integration/web/test_shareable_reviews_routes.py`, `tests/unit/web/auth` with `-n 0` — PASS.
- [ ] **Step 5: Commit** `feat(share): import a shared graph and its files into a new session`.

### Task 8: Frontend API and store

**Files:**
- Modify: `src/elspeth/web/frontend/src/api/shareableReviews.ts`, `src/elspeth/web/frontend/src/types/api.ts`, `src/elspeth/web/frontend/src/stores/shareableReviewStore.ts`
- Test: `src/api/shareableReviews.test.ts`, `src/stores/shareableReviewStore.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type ShareLifetimeSeconds = 86400 | 604800 | 2592000;
  export interface MarkReadyOptions { allowImport: boolean; lifetimeSeconds: ShareLifetimeSeconds }
  export async function markReadyForReview(sessionId: string, options: MarkReadyOptions, signal?: AbortSignal): Promise<MarkReadyForReviewResponse>
  export async function fetchShareableFiles(sessionId: string, signal?: AbortSignal): Promise<ShareableFilesResponse>
  export async function importSharedSnapshot(token: string, signal?: AbortSignal): Promise<SharedImportResponse>
  // SharedInspectResponse gains import_allowed: boolean; files: { filename: string; size_bytes: number }[]
  // store: RequestPhase gains "configure"; openConfigure(sessionId) loads files and opens the dialog without minting;
  //        confirmMark(options) mints. openAndMark is removed and its CompletionBar call sites switch to openConfigure.
  ```
  `importSharedSnapshot` reuses `fetchSharedInspect`'s 401 handling: only a bare `WWW-Authenticate: Bearer` 401 logs out. Each validator rebuilds its object from an explicit field list — add the new fields to both the check and the literal.

- [ ] **Step 1: Write failing tests:** request body sent for `markReadyForReview` (`{"allow_import": true, "lifetime_seconds": 86400}`); validators accept the new inspect fields and reject a missing `import_allowed`; `importSharedSnapshot` on a capability 401 does not log out (Review Focus 4); store `openConfigure` does not call `markReadyForReview`.
- [ ] **Step 2: Run** `cd src/elspeth/web/frontend && npx vitest run src/api/shareableReviews.test.ts src/stores/shareableReviewStore.test.ts` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `npm run typecheck` — PASS.
- [ ] **Step 5: Commit** `feat(web): share options and import calls in the frontend API`.

### Task 9: Share dialog configure step

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.tsx`, `CompletionBar.tsx`
- Test: `SaveForReviewDialog.test.tsx`, `CompletionFlow.integration.test.tsx`

**Interfaces:**
- Consumes: Task 8 store (`openConfigure`, `confirmMark`, files state).
- UI: checkbox "Let recipients import a copy" (unchecked by default; disabled with `not_importable_reason` shown when `importable` is false); when checked, the file list (filename, human-readable size, `used_by`); radio group "Link expires after" — 1 day / 7 days / 30 days, default 30 days; button "Create link". Success state unchanged.

- [ ] **Step 1: Write failing tests:** opening shows the configure step and makes no mint call; ticking import reveals the file list; "Create link" sends the chosen options; a not-importable graph disables the checkbox and shows the reason; existing success/error/retry tests still pass after their setup moves to the configure step.
- [ ] **Step 2: Run** `npx vitest run src/components/composer/SaveForReviewDialog.test.tsx src/components/composer/CompletionFlow.integration.test.tsx` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same plus the a11y component tests (`src/test/a11y/components.a11y.test.tsx`) — PASS.
- [ ] **Step 5: Commit** `feat(web): choose import and lifespan before creating a share link`.

### Task 10: Import button, navigation and provenance notice

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx` (also its docstring: no longer "not a fork surface")
- Modify: `src/elspeth/web/frontend/src/components/common/AppNoticeCenter.tsx` (new kind `"shared-import"`), `src/elspeth/web/frontend/src/App.tsx`
- Test: `SharedInspectView.test.tsx`, `AppNoticeCenter.test.tsx`, `App.test.tsx`

**Interfaces:**
- Consumes: `importSharedSnapshot`, inspect `import_allowed`/`files` (Task 8).
- Behaviour: when `import_allowed`, render "Import a copy into my workspace" plus the file list (name and size) after the banner, outside any `useReadOnly` consumer. Click → disable the button, call the API, then `useSessionStore.getState().loadSessions()` and `window.location.hash = \`#/${session_id}\``. Errors: capability 401 → "This share link has expired"; 403 → "This link doesn't allow importing"; 404 → the existing "ask the sender for a fresh link"; 413 → storage-quota message. In the composer, when the active composition state's `composer_meta.shared_import` exists, `App.tsx` pushes an `info` notice of kind `shared-import`: "Imported from a link shared by {shared_by_username}."

- [ ] **Step 1: Write failing tests:** button absent for view-only links; click imports, reloads sessions and sets the hash; button disabled while in flight; expired-link 401 shows the message and the user stays signed in (Review Focus 4); the notice appears for a state with `shared_import` meta and not otherwise.
- [ ] **Step 2: Run** `npx vitest run src/components/shared/SharedInspectView.test.tsx src/components/common/AppNoticeCenter.test.tsx src/App.test.tsx` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same plus `npm run typecheck` and `npm run lint` — PASS.
- [ ] **Step 5: Commit** `feat(web): import a shared graph from the shared view`.

### Task 11: End-to-end, docs and release gate

**Files:**
- Create: `src/elspeth/web/frontend/tests/e2e/shared-graph-import.spec.ts`
- Modify: `src/elspeth/web/frontend/tests/e2e/setup/global-setup.ts` (register a second user, `e2e-recipient`)
- Modify: `CHANGELOG.md` (`## Unreleased (0.8.10)`: the feature, the storage-growth cost with dta-au/elspeth#356, the LLM-authored-file refusal, and the accepted crash/retry gaps)
- Modify: `docs/superpowers/specs/2026-11-09-shared-graph-import-design.md` (§2: plugin-policy violations are **refused** by the seeder with 422, as library fork does; LLM-authored files are not importable)

- [ ] **Step 1: Write the Playwright spec:** the sharer seeds a session with an uploaded CSV source and a `reference_join` file (helpers `createSession`, `uploadBlob`, `seedCompositionState` in `tests/e2e/helpers/api.ts`), creates a 1-day import link through the dialog; the recipient opens it, clicks Import, lands in a session whose graph shows both files attached.
- [ ] **Step 2: Run** `cd src/elspeth/web/frontend && npx playwright test tests/e2e/shared-graph-import.spec.ts --trace off` — PASS.
- [ ] **Step 3: Update** the changelog and the spec lines above; run `.venv/bin/python -m pytest tests/unit/docs -n 0` (the ADR commit-citation test fails in shallow clones; compare against the base commit).
- [ ] **Step 4: Run the gates:** `scripts/full-suite-gate.sh --execute --detach` and read its `summary.txt`; if Docker is available, `pytest tests/ -m testcontainer -n 0`; keyless `elspeth-lints` corpus compared before/after; `cd src/elspeth/web/frontend && npm test`.
- [ ] **Step 5: Commit** `test(e2e): share and import a graph with its files` and `docs: record shared graph import for 0.8.10`.
