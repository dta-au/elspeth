# Bounded Composer Blob Discovery Implementation Plan

> Planning only, 2026-11-09. This lane wrote this file only. No implementation, test, scan, provider call, installation, commit, or Page operation was performed. For implementation, use the execution method already authorized by the parent; do not start overlapping work until the identity dependency below lands.

**Goal:** Remove BOS-01's undiscoverable oldest files and XC-13's premature planner coverage through bounded, truthful continuation on the existing two Composer tools and browser file panel.

**Architecture:** Make the blob service own one deterministic inventory query, with distinct all-status and ready-only projections. Add a versioned page envelope and live keyset continuation. Planner completeness is a fact about an observed, contiguous traversal, never a property inferred from a terminal page alone and never a snapshot/absence guarantee.

**Tech stack:** Python, SQLAlchemy, strict Pydantic producer contracts, existing FastAPI session routes, React/Zustand/TypeScript, SQLite unit tests and PostgreSQL testcontainers.

**Spec:** `/workspace/elspeth/docs/architecture/composer-tool-assessment/remediation-plan.md` R2; `tools/list_blobs.md`; `tools/list_composer_blobs.md`; README BOS-01/XC-13 and evidence record. These audit documents are in the main checkout, not the integration worktree.

**Design review incorporated:** `blob-pagination-design-review.md`, including the parent's rulings: retain ordinary REST metadata/output credential policy; use live keyset, fixed-limit scope and a 30-minute application-HKDF cursor; enforce request-owned scope matching and active generation-aware planner gaps. These are explicit accepted design choices, not new approval gates or claims of runtime verification.

**Plan ready:** the final scoped static rereview found no design blockers. Its two follow-through points are incorporated below: the aggregate state cap includes retained manifest history and reserves capacity before coverage publication, and wire privacy forbids separately serialized private position fields rather than the ordering tuple legitimately encoded in an opaque unencrypted cursor. Implementation still requires identity integration/reinspection; this status is design readiness, not implementation or test evidence.

## Baseline, limits, and dependencies

- Source inspected: `/workspace/elspeth/.claude/worktrees/composer-remediation-landscape-metadata`, HEAD `ec079378e1baeff8db974ccb1f94f1f942edd6d4`. `git status --short` returned no entries both at initial inspection and before writing this plan. Source anchors below describe that HEAD; they are not runtime exposure measurements or test results.
- Read root AGENTS.md and CONTRIBUTING.md, including whole-tree gates, Composer invariants, trust domains, frontend and compatibility conventions. AGENTS requires project artifacts inside the project; this requested lane artifact is intentionally ignored coordination state, never stage it.
- **Hard dependency:** the active blob identity lane based on `1bc` must land before implementing this plan. Reinspect its selectors, canonical ID type, planner key admission, custody, service/context construction, selected responses and tests. Use its canonical selector once, including cursor session/blob IDs; do not revive raw-string identity branches or reapply the pre-identity code in these anchors.
- Retain ready-only eligibility for `list_composer_blobs`; all statuses remain in `list_blobs` and REST/file panel. Do not add MIME, size or provenance eligibility filters in this slice. A ready-only inventory is not certification that every listed blob can be wired to every option. Existing downstream readiness, hash, provenance, custody, retention and authorization checks remain authoritative.
- Composer discovery projections contain metadata only: no blob bytes, previews, storage paths or descriptions; no credential probes or provider calls. Keep whole-emitted-page credential refusal and owned-row integrity failures; never drop an unsafe row and declare the remainder complete. New and legacy REST listings retain ordinary `BlobMetadataResponse` visibility, including its existing source description, timestamps and provenance, and retain existing REST output credential policy; do not add a REST output guard by analogy with Composer.
- No new tool names or MCP exposure. No parked graph paging, general content reader overhaul, broad error catalogue cleanup, policy changes, store migration, index migration, quota/custody rewrite, or discovery-budget increase.
- Source inspection establishes seams, not coverage. Historical audit evidence reports the 51/50 reproduction, SQLite tied-order behavior and scripted planner notice; its PostgreSQL tied-order and browser checks were not run. This lane did not rerun those probes.

## Current route and contract seams

| Surface | Actual source seam and present behavior | Required change |
|---|---|---|
| Canonical tools | `web/composer/tools/blobs.py`: `_sync_list_blobs` (534) and `_sync_list_ready_blob_inline_descriptors` (559), handlers at 587/617, declarations at 604/644 | Remove duplicate fixed-50 query authority; retain projection mapping and credential admission |
| Existing service | `web/blobs/service.py:2864` `BlobServiceImpl.list_blobs(session_id, limit=50, offset=0)`; contracts `contracts/blobs.py:534`, reexports `web/blobs/protocol.py` | Preserve existing internal/unbounded callers; share one ordered query builder with a new bounded page API, not a second independent SQL implementation |
| Freeform | `_registry.py`, `_dispatch.py` semantic/schema admission; `tool_batch.py` worker execution and `admit_discovery_result` | Same tool names/kind, unchanged read-only/provider path; admit new page before audit/model serialization |
| Planner | `capability_skill.py` advertises both tools; `pipeline_planner.py` `_tool_information_keys` 435, `_resolved_discovery_information_keys` 514, manifest `with_result` 292, discovery loop 4876/5080/5101 | Normalize arguments, track individual page progress and contiguous traversal; terminal-page-only and failure must not close inventory gaps |
| Response admission | `tools/blobs.py` `_BlobInventoryItem`, `_ComposerBlobsResponse`, parse/encode functions 295/319/359/374 and selected contracts 391 | Strict v2 producer shape, consistency/invariant checks; preserve item field visibility |
| Redaction/audit | `redaction.py` reasons 2955/2974 and manifest entries 3985/3992; audit records tool arguments and envelope | Replace no-arguments reasoning; retain limit/cursor and page fields without unknown-key redaction; test actual persisted/model-observed envelope equality |
| REST | `web/blobs/routes.py:360` GET collection returns `list[BlobMetadataResponse]`; `limit` 1..500, `offset` >=0; `_verify_session_and_get_blob_service` enforces user/session ownership | Preserve legacy route, add explicit page route ahead of `/{blob_id}`; never derive ownership from a cursor |
| File panel | `frontend/src/api/client.ts:1573` `listBlobs` sends no paging args and casts JSON via `parseResponse<BlobMetadata[]>`; `stores/blobStore.ts` publishes one result; `components/blobs/BlobManager.tsx` labels loaded count as Files | Use v2 endpoint and runtime decoder, store explicit continuation, expose Load more and Refresh; count loaded rows honestly |
| Tool UI | `toolCallDescriptions.ts:49-50`, `ToolCallCard.tsx` | Update wording only; current cards do not decode/render inventory data |
| MCP | `composer_mcp/server.py` imports `_DISCOVERY_TOOLS` and `_MUTATION_TOOLS`, builds `_COMPOSER_TOOL_NAMES` at 119; dispatch membership at 396; blob tools are `BLOB_DISCOVERY`, separate from those tables | Keep excluded; verify actual emitted definitions and rejected dispatch in future tests. Do not invent a blob MCP route or claim byte parity for an absent route |

## Contract decisions (recommended routine choices)

### Page request and deterministic query

Both existing tool names accept a closed argument object with optional `limit` (strict integer 1..500, default 50) and `cursor` (nonempty bounded string, max 1024); `{}` means first page of 50. Reject bool/float/string limits, null cursors, extra keys and malformed tokens. Strict provider projection must translate omitted semantic optionals consistently; null wire sentinels, if generated by the established projection, decode to omission rather than becoming extra semantic forms.

Introduce owned `BlobInventoryProjection = Literal["session", "composer"]`, `BlobPageRequest`, `BlobPagePosition` and `BlobRecordPage` in `contracts/blobs.py`. Proposed service interfaces:

```python
async def list_blob_page(self, session_id: UUID, *, projection: BlobInventoryProjection,
                         limit: int = 50, cursor: str | None = None) -> BlobRecordPage: ...
def list_blob_page_sync(self, session_id: UUID, *, projection: BlobInventoryProjection,
                        limit: int = 50, cursor: str | None = None) -> BlobRecordPage: ...
```

Use the landed identity type in place of UUID above if that lane establishes one. `BlobRecordPage` owns `records: tuple[BlobRecord, ...]`, `limit`, `has_more`, `next_cursor`, normalized projection and canonical session identity; wire serialization is adapter-owned. Perform filtering in SQL **before** limiting, fetch `limit + 1`, validate owned rows with the service's existing literal/record admission, emit at most limit. Ready rows still require a non-null authoritative hash for the inline projection.

One total order: `created_at DESC, id ASC` using the actual persisted string ID and database order. Next-page predicate is `created_at < last_created_at OR (created_at = last_created_at AND id > last_id)`. Generate the cursor from the last emitted row, never the lookahead. Serialize persisted timestamps as UTC with exactly six fractional digits; attach UTC to SQLite-naive values, convert already-aware offsets to UTC, and rebind without a float round trip or precision loss. Cover adjacent microsecond boundaries as well as ties. Use the identity lane's canonical persisted IDs; do not normalize a dirty stored ID into a different SQL ordering value. Assert actual selected SQLite/PostgreSQL order, not a Python-sort inference about collation. Apply the same total order to legacy `list_blobs`, retaining `limit=None` and offset semantics for fork/internal callers.

### Cursor trust and bounded lifetime

Accepted policy: authenticated opaque cursor, not an offset and not a caller-authored SQL selector. Add `web/blobs/inventory_cursor.py` containing concrete `BlobInventoryCursorCodec.encode(position) -> str` / `decode(token, *, session_id, projection, limit) -> BlobPagePosition`. Freeze the closed payload schema as `version: Literal[1]`, canonical `session_id`, projection, strict integer `limit` 1..500, `order: Literal["created_at_desc_id_asc_v1"]`, fresh-root `traversal_nonce` (32 lowercase hexadecimal characters from 16 random bytes), `last_created_at` (UTC string with six fractional digits), canonical persisted `last_id`, and strict integer Unix-second `started_at`/`expires_at`. Reject booleans/coercion, unknown keys, invalid timestamps/noncanonical encodings; require `expires_at == started_at + 1800`, `started_at <= now < expires_at`. Inject a clock in codec tests; do not accept arbitrary clock skew as freshness. Retain nonce/start/expiry through continuations without renewal.

The exact **non-JWT** byte format is `base64url(canonical_json(payload).encode("utf-8"))` without padding, followed by one `.` and the 64-character lowercase hexadecimal HMAC-SHA256 of that ASCII base64url component. Enforce deterministic canonical encoding, re-encoding equality, length <=1024 before decoding, and constant-time signature verification. The same continuation payload yields the same token. A fresh root gets a new nonce without a server registry; this separates roots but randomness is not discovery information. This is opaque to clients but **not encrypted**, and is not an authorization bearer. Include no filenames, paths, credentials or signing material. No general token framework.

Derive a dedicated key from existing web `secret_key` through the existing HKDF authority in `key_derivation.py`, info `b"elspeth-blob-inventory-cursor-v1"`; do not alter existing derivation labels, reuse auth/share-link signing keys directly or add operator configuration. This is application runtime derivation, unrelated to `ELSPETH_JUDGE_METADATA_HMAC_KEY`, signed lint allowlists or operator clearance; implementation/tests need no real secret. Wire the codec into the production `BlobServiceImpl` at `web/app.py` and make the configured page service available to Composer context. A missing codec on a configured page service is a framework wiring fault, never a fallback query/default key. Existing directly constructed services using only legacy methods need no codec. Key rotation or expiry gives restart-first-page guidance on tools and Refresh in the UI. Fixed-limit binding and 30 minutes are accepted usability/replay policies, not freshness proofs or security necessities imposed by keyset SQL.

`contracts/credential_material.py` rejects JWT-shaped text: pass actual issued non-JWT tokens through argument and response credential guards, normal provider wire decode, redaction and audit serialization in tests. Do not exempt arbitrary `cursor` strings from scanning, and keep filename/other metadata scanning intact. A signed token is not an excuse to disclose arbitrary caller text before validation; a credential-shaped caller cursor may be refused by the existing argument guard before codec parsing.

Reject bad signature, malformed/oversized/base64/JSON/type-invalid tokens, unsupported version/order, expired/future issuance, wrong session, projection or limit. Cursor expiry, not DB mutation, defines stale tokens. A valid cursor whose pivot row was deleted remains usable from its authenticated ordering tuple; do not look the pivot up and create a deletion dead end. REST authentication/session checks precede cursor-dependent inventory access on every request. Signed session binding is defense in depth, never authorization. A narrow cursor-invalid exception maps to existing invalid-argument machinery (`ToolArgumentError` on tools; generic REST 422 with restart guidance), without a general error-catalogue project. Never translate DB/owned-row integrity failure, missing codec or mismatched first-party producer scope into caller error.

### Explicit live traversal semantics

`consistency="live_keyset"`, fixed order, **no snapshot promise**. With immutable ordering keys, an already-emitted surviving row does not repeat due to insertion/deletion ahead of the cursor. New rows ordered ahead of the cursor and rows becoming ready ahead of a ready-only cursor may be omitted until Refresh/restart. Newly eligible/newly inserted rows behind the cursor may appear; rows deleted or no longer eligible before observation may disappear. Metadata is observed per page and may change between pages. Backdated rows behind the cursor can extend the traversal; client/planner limits may stop it. No exact total count is returned. `has_more=false` means that page's query found no further matching row behind its position at observation time. It does not mean no newer or concurrently changed eligible row exists.

Reinspect the landed writers before relying on immutable keys: replacement commit retains ID/created_at while changing metadata; output finalization changes status/hash/size. Even a refreshed live traversal cannot prove unconditional current absence under continuing mutation; refresh is a way to seek omitted arrivals, not a snapshot operation.

### Wire and compatibility boundary

Successful tool data is the explicit v2 object:

```json
{"schema_version":2,"session_id":"<canonical-session-id>","blobs":[],"page":{"limit":50,"has_more":false,"next_cursor":null,"consistency":"live_keyset","projection":"session","order":"created_at_desc_id_asc_v1"}}
```

Both tools use this envelope with canonical session echo even for empty pages and their existing distinct item shapes (`id` vs `blob_id`, ordinary provenance/status vs inline hash); the ready-only tool uses `projection="composer"`. `list_composer_blobs` retains `data.blobs`, but the required new outer fields are not universally additive for strict consumers. **`list_blobs` changes data from bare list to versioned object**, an intentional public response change requiring release/reference notes and updating all live callers/fixtures. Do not infer a release number. Existing `{}` requests still work and clearly disclose continuation. Producer admission accepts v2 only: exact keys/types, limit/count bound, correct projection, consistency/order literals, `has_more == (next_cursor is not None)`, no duplicate canonical IDs, no content fields. Adapter admission compares the service-returned session, projection and normalized limit with request-owned values; a well-formed mismatch is a producer fault. Do not infer completeness from a legacy bare list, len=50, or an old `{blobs: ...}` object.

Persisted v1 audit/message data is historical evidence, remains readable and is not rewritten. Any replay/display path that parses it must have an explicit legacy reader yielding `completeness=unknown`; it must never feed v1 data into v2 producer admission or mint planner coverage. Current source does not have a tool-specific browser inventory decoder; add no broad chat history migration. Locate any existing replay consumer at implementation reinspection and test only actual consumers. Do not claim all historical responses replay through the new producer parser.

REST adds `GET /api/sessions/{session_id}/blobs/page` with limit/cursor, the same v2 shell/session echo and ordinary existing `BlobMetadataResponse` items (`web/blobs/schemas.py:47–83`), including source description, timestamps and provenance. Register it before `/{blob_id}`. It is a **new REST route**, not a new Composer/MCP tool. Retain existing REST output credential policy: `_blob_response` mapping, no new tool-style output refusal. Keep existing collection GET response and limit/offset parameters unchanged; document that older clients must explicitly page offsets and lack the new truth signal. Browser `listBlobPage` uses the new route with a decoder for the actual closed REST item schema; it compares both envelope and every item's session with the captured request session. Do not silently change legacy `listBlobs(): Promise<BlobMetadata[]>` into a one-page v2 cast or an unbounded eager fetch. Echo is detection, never ownership authority.

### Private owned positions for planner progression

**Decision:** the planner obtains authenticated ordering positions through a narrow method on the **same configured blob service**, then carries them in the existing planner-local audited result carrier. It must not infer `created_at` from item fields (the Composer item shapes do not contain it), decode opaque token JSON itself, acquire a signing key, or add position/timestamp fields to the wire envelope. A service-only API reuses the single configured codec's parse/authenticate implementation; there is no second untrusted parser.

Add these frozen private-accounting DTOs to `contracts/blobs.py` and expose the method on the existing `BlobService` protocol, reexported by `web/blobs/protocol.py`:

```python
@dataclass(frozen=True, slots=True)
class BlobInventoryOrderKey:
    created_at: datetime  # aware exact UTC, persisted microsecond precision
    blob_id: str         # actual canonical persisted SQL ordering value

@dataclass(frozen=True, slots=True)
class BlobInventoryPagePositions:
    request_after: BlobInventoryOrderKey | None
    next_after: BlobInventoryOrderKey | None

def inspect_blob_page_positions_sync(
    self, session_id: UUID, *, projection: BlobInventoryProjection, limit: int,
    request_cursor: str | None, next_cursor: str | None,
) -> BlobInventoryPagePositions: ...
```

Use landed identity types instead of the displayed UUID/string where applicable without changing the persisted SQL order. `BlobServiceImpl.inspect_blob_page_positions_sync` delegates to its configured `BlobInventoryCursorCodec`; it authenticates each non-null token and checks canonical payload/schema/session/projection/limit/order. If both tokens exist, require identical traversal nonce/start/expiry and strict forward movement under the mixed-direction total order. Return only the two owned order keys, not nonce, signature, expiry, codec/key objects or token payload mappings. None means no request/next pivot: a terminal root has `(None, None)`, a terminal continuation has `(request pivot, None)`. Terminal items still contribute their existing selected metadata to observation fingerprints; no fabricated final-row timestamp is needed.

This method performs **no DB/filesystem/provider access** and grants no inventory-read authority. Factor the codec so request admission verifies authentication/scope **and live expiry**, while this post-success inspection reuses authentication/schema/scope parsing without re-testing the wall clock. The original list call already enforced expiry before querying; a token expiring between successful query and post-success accounting must not retroactively invalidate that observation. An expired token still cannot start a new list query. Call this inspector only for successful, selected-admitted results in the production planner flow. Invalid authored tokens remain handled by the listing admission; an unauthentic/mismatched token discovered inside a claimed successful producer result is a framework producer fault, not a recoverable new caller error.

**Exact planner call graph at the inspected baseline:** `plan_pipeline` builds `request_context` at 3429; nested `execute_discovery` calls `execute_discovery_tool_with_context` through `run_planner_sync`, checks unchanged state, calls `admit_discovery_result`, and returns `_AuditedDiscoveryResult(result, admitted)` at 4917. `dispatch_with_audit` surrounds that body. `_AuditedDiscoveryResult` (1564) currently holds only result/admitted; `execute_one_discovery` unwraps it at 4959, and the authored-result loop at 5000 receives `AdmittedDiscoveryResult` before serializing and updating manifest state. Implement the following narrow additions, after reinspection on the identity-integrated HEAD:

1. Add `blob_inventory_positions: BlobInventoryPagePositions | None = None` to this **planner-local** `_AuditedDiscoveryResult`; do not change common `ToolResult`, `AdmittedDiscoveryResult`, `AdmittedResponse` or any `to_dict()`/`to_wire()` serialization for this bookkeeping. Its existing `to_dict()` explicitly returns `self.admitted.to_dict()`; `audit.py` dispatch serialization calls that method rather than dumping the dataclass. Preserve that exact boundary so the new private field cannot leak through an automatic dataclass encoder.
2. Inside nested `execute_discovery`, **after successful selected response admission and request-owned v2 session/projection/limit matching**, call `await run_planner_sync(partial(request_context.blob_page_service.inspect_blob_page_positions_sync, ...))` for the two list tools. Inputs are canonical request session/projection/limit, admitted semantic request cursor and selected-admitted `page.next_cursor`. Use the service already threaded by Task 2; no codec or key is passed to the planner. Return the positions in `_AuditedDiscoveryResult`. Other tools and failed discovery have None. Keeping inspection inside the existing audit dispatch means a producer-position fault is recorded before any tool success is finalized.
3. Extend the nested `execute_one_discovery` result tuple with the optional owned positions; argument rejection returns None. Preserve the existing task-drain and authored ordering checks. In the authored-result loop pass that value to `BlobInventoryTraversalTracker.observe(..., positions=...)` before computing post-admission gain/current coverage. Require positions for successful v2 list results; missing positions are an owned wiring fault, not assumed progress.
4. Only original admitted wire data enters provider serialization, visible tool events, audit payloads and messages; local position DTOs stay invocation-private. Fingerprints use `request_after`/`next_after` plus selected item fields/terminal status, not a fresh parse of raw cursors. Exact expected-token matching still checks chain provenance; positions supply order/gain facts rather than replacing that check.

This adds no public response fields or new signing-key custody. REST and freeform tool handlers keep the same v2 shapes and do not need planner accounting carriers. The inspector is an internal protocol method, not an HTTP/MCP/model tool.

### Task 1 — Service paging authority and authenticated continuation

**Files:** modify `contracts/blobs.py`, `web/blobs/protocol.py`, `web/blobs/service.py`, `web/key_derivation.py`; create `web/blobs/inventory_cursor.py`; use a focused `web/blobs/inventory.py` query helper if needed to keep cursor parsing separate from service lifecycle code. Modify `web/app.py` wiring. Tests: new `tests/unit/web/blobs/test_inventory_paging.py`, `test_inventory_cursor.py`, existing `test_service.py`, `tests/unit/web/test_key_derivation.py`, `test_key_derivation_wiring.py`.

**Produces:** page interfaces/DTOs above, configured shared service, legacy API intact, and `inspect_blob_page_positions_sync(...) -> BlobInventoryPagePositions` using that service's existing configured codec (no extra key owner).

- [ ] Write failing tests for 0/1/50/51/1001 inventories, limit=1/500, complete traversal equals independently selected authorized IDs/order in an unchanged DB, equal-timestamp rows straddling every boundary and reverse insert order, adjacent microseconds and aware offsets, no duplicate/missing ID. Seed ties directly and assert the actual DB order/timestamps; do not substitute a Python sort for a collation proof.
- [ ] Write failures for all cursor/request classes above, strict integer/time/encoding schema, deleted pivot continuation, separate sessions, separate ready/all scopes and deterministic timestamp precision. Same payload/key yields identical two-component non-JWT token; fresh roots differ by nonce while continuation preserves nonce/start/expiry. Verify 30-minute boundary/key-rotation restart behavior, limit+1 lookahead admission without exposure and no cursor on empty terminal results. Validate fetched lookahead rows as owned records, but credential-scan only the emitted Composer projection; never skip a credential-bearing emitted row and advance beyond it.
- [ ] Inspector tests use actual issued tokens and the same service instance: authenticate/scope-match both positions, retain exact DB ordering values, reject mismatched roots or nonadvancing next pivot, return `(None,None)` for terminal root and `(request_after,None)` for terminal continuation. Assert no DB/filesystem call and no key/nonce/token-payload exposure. A successful request observed just before expiry can still have its positions inspected afterward, while a new list request with that expired token refuses; inspection never authorizes an inventory read.
- [ ] Implement one shared filter/order authority and signed codec. Preserve existing `list_blobs(limit=None)` and offset callers and `_row_to_record` integrity behavior. No content/filesystem read and no new long-lived DB transaction/session lock across pages.
- [ ] Run focused tests with explicit worktree imports; inspect complete logs and exit codes. Commit only Task 1 files once authorized execution reaches that checkpoint.

## Execution verification and release limits

During implementation, use a fresh isolated tree after identity integration; pin actual HEAD and both import roots. Example focused Python command shape (not run here):

```bash
cd "$WT" && PYTHONPATH="$WT/src:$WT/elspeth-lints/src" "$WT/.venv/bin/python" -m pytest -n 0 tests/unit/web/blobs/test_inventory_paging.py tests/unit/web/blobs/test_inventory_cursor.py tests/unit/web/composer/test_blob_response_contracts.py tests/unit/web/composer/test_blob_inventory_progress.py tests/unit/web/composer/test_pipeline_planner.py
```

Write logs to a lane-private path and inspect exit codes/full output per AGENTS; verify `elspeth.__file__` and `elspeth_lints.__file__` resolve into the implementation tree. Frontend commands run from `$WT/src/elspeth/web/frontend`: `npm run typecheck`, `npx vitest run` with changed test files, and the installed Playwright command with the new test path. Test authors replace the proposed new filenames only if repository conventions require it and update this task record accordingly.

The service SQL/contract and context changes require affected whole-tree gates, including `tests/unit/architecture/test_session_db_mutation_authority.py` (existing query connection fingerprints), `tests/unit/contracts/test_web_blob_fencing.py` (REST/service source pins), wire fidelity/projection/decode, selected response/envelope/teaching, redaction snapshot and declared oracle pins. Inspect reviewed files in full; regenerate only honest pins through canonical tooling and never sign lint metadata without operator authority. Use the canonical full-suite gate for the final frozen production tree, including ruff/mypy/contracts/lints/default pytest and serial `pytest tests/ -m testcontainer -n 0` as required. Coordinate broad suite ownership with sibling lanes; no parallel full suites.

Measure actual exposure/serialized evidence and run acceptance tests before updating BOS-01/XC-13 disposition. A static seam inventory, mocked UI, SQLite run, or historical audit result cannot establish current PostgreSQL, browser-integrated, provider or deployment coverage. This slice deliberately leaves legacy REST consumers on their explicit offset API and live discovery without snapshot guarantees. No production implementation is authorized by this planning artifact alone.


## Parent execution handoff

Prepared only; parent will supply final base/worktree after current wave freezes. Identity Task1 is locally integrated05bcab939; inspect canonical_resource_uuid without broadening normalization of persisted ordering IDs. Implement only Task1 service/codec/production service wiring and its focused tests. Do not implement adapters, planner tracker, browser, or full PostgreSQL Task5 in this step. Expose and test the exact private position inspector now for later consumers. No subagents/commits/pushes/providers/installs; no real secrets, signing or migrations. Parent will assign test capacity before launch. Return full report and complete patch including new files to central pagination-service-report.md and pagination-service-review.diff, frozen for fresh Astra review.
