# Blob inventory pagination implementation brief

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` according to the parent's preserved execution method, not a newly selected method. This is a bounded preparatory brief; rebind and review before implementation.

**Goal:** Make every session blob reachable through bounded existing tool/REST/browser surfaces and make planner inventory coverage truthful.

**Architecture:** Explicit opt-in live keyset pages preserve legacy roots. One blob-service query authority feeds distinct content-free tool projections and the existing REST projection. Request-local planner evidence proves a sequential root-to-terminal traversal.

**Tech stack:** Python/SQLAlchemy/Pydantic/FastAPI; TypeScript/React/Zustand; existing pytest/PostgreSQL and frontend/browser infrastructure.

**Spec:** `pagination-design.md` in this lane. Source baseline `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`; no implementation/test claim. D1/D2 and `resource-identity-brief.md` precede this work. User's execution method: Sol 6.1 high implementation, Astra independent review, coordinated by parent.

## Global constraints

- Existing `{}` tool and legacy REST roots remain exact. Tool `page` object has limit 1..50 and nullable cursor; REST explicit cursor mode keeps limit 1..500. Cursor maximum 512 ASCII chars, strict v1 fields as in design.
- Ordering is `created_at DESC, id DESC`; all and ready predicates never merge. Page query selects at most limit+1; continuation uses last returned row.
- Live enumeration, no snapshot/revision/TTL promise, no cursor authorization, no new transport/MCP tools, no database migration.
- Keep D1 strict response admission, original historical invocation/hash authority, credential refusal and Tier-1 faults. No hidden page calls, cache opt-in or budget changes.
- Parent owns broad-test capacity. Use explicit worktree/PYTHONPATH provenance and canonical full-suite workflow after integration, never simultaneous suites.

## Review focus

- A valid terminal page without earlier pages cannot close a whole-inventory key (task 3).
- A fresh root cannot inherit previously supplied inventory completeness (task 3).
- A ready transition behind the cursor is a documented live omission, not hidden snapshot completeness (tasks 1, 3, 4).
- A deleted cursor row must not prevent continuation; a foreign cursor must not broaden scope (tasks 1, 2).
- An upload/delete/session switch while loading more must not publish stale or cross-session rows (task 4).

## Task 1 — Shared page authority

Files: create `src/elspeth/web/blobs/inventory.py`; modify `src/elspeth/web/blobs/service.py`, `src/elspeth/contracts/blobs.py` for owned DTOs/protocol signature, and `src/elspeth/web/blobs/protocol.py` for conventional re-exports. Tests: existing `tests/unit/web/blobs/test_service.py` plus focused new inventory tests; PostgreSQL coverage in existing web testcontainer layout.

Interfaces: nominal `BlobInventoryCursor`, `BlobInventoryPage` in contracts (tuple of guarded BlobRecord, limit, requested/next cursor, scope; cross-field invariants); `decode_blob_inventory_cursor(value: str, *, session_id: UUID, scope: Literal["all","ready"], limit: int) -> BlobInventoryCursor`; `encode_blob_inventory_cursor(cursor) -> str`; `list_blob_inventory_page_sync(engine: Engine, *, session_id: UUID, scope, limit: int, cursor: BlobInventoryCursor | None) -> BlobInventoryPage`. Add async `BlobServiceImpl.list_blob_page` wrapper and declare its contract at the existing protocol's owning layer. Nominal DTOs are in contracts and wire codec in web; no contracts→web dependency. Legacy list signature remains.

- [ ] Rebind source against integrated base and reproduce 51-row truncation/tie controls through existing SQL authorities; record exact commit/import provenance.
- [ ] Write failing page/cursor tests from design: exact ordered 101-row traversal, both scopes, legacy offset ordering, cross-session exclusion, malformed token table, deleted-anchor control.
- [ ] Run only those tests and record expected failures.
- [ ] Implement page DTO/codec and common session/order query authority around `_row_to_blob_record`; both legacy list and new pages reuse predicate/order construction.
- [ ] Run focused tests and real PostgreSQL interleavings specified in design, under capacity coordination. Mutate tie-breaker, session predicate, status predicate and boundary/lookahead to prove assertions discriminate.
- [ ] Review and checkpoint bounded diff; no claim of tool/browser closure from query-only work.

## Task 2 — Opt-in tool and REST contracts

Files: `src/elspeth/web/composer/tools/blobs.py`, `src/elspeth/web/composer/redaction.py`, `src/elspeth/web/blobs/routes.py`, `src/elspeth/web/blobs/schemas.py`; exact declaration/contract snapshots and response tests touched by these producers. Tests: `test_tools_blobs.py`, `test_blob_response_contracts.py`, `test_blob_inline_tools.py`, `test_tool_declarations.py`, wire projection/decode suites, `tests/unit/web/blobs/test_routes.py`, redaction/envelope/audit tests.

Interfaces: both tools accept omitted page or closed `{page:{limit,cursor}}`; new response exactly `{blobs, page:{schema_version,consistency,limit,cursor,next_cursor,has_more}}`. Each tool retains original item whitelist. REST uses `pagination=cursor`, rejects offset combination and standalone cursor, and preserves legacy list mode. Cursor service errors map to tool argument refusal/REST422 with safe fixed detail.

- [ ] Add failing actual producer/dispatch tests for both old/new modes, both dialects, both inventories, REST modes and credential/null-hash controls.
- [ ] Implement shared service consumption and exact legacy/v1 response variants; no unconstrained union/fallback and no extra item fields. Extend argument schema/redaction/selected encoders together.
- [ ] Prove invalid new shapes fail before provider egress and recorded legacy envelopes remain byte/hash compatible. Use real producers for schema/redaction snapshot updates.
- [ ] Run affected contracts, redaction and audit tests; verify no MCP exposure, no cache opt-in, no authored effects. Mutate has_more/next relation, unknown page version and item whitelist to ensure rejection tests fail.
- [ ] Review and checkpoint contract slice with task1; do not mark BOS-01 closed until planner/browser work is present.

## Task 3 — Planner traversal evidence and selector identity

Files: create focused `src/elspeth/web/composer/blob_inventory_evidence.py`; modify `pipeline_planner.py`; tests in `tests/unit/web/composer/test_pipeline_planner.py` and a focused evidence test module. Teaching changes in task4 must describe this behavior.

Interfaces: a request-owned traversal object consumes admitted call/page pairs in delivered order and exposes exact expected continuation plus whether that scope completed. It returns page observation keys and only emits the existing whole-scope key after linked completion. Its root-reset operation removes prior whole-scope supply before execution. Scope is all versus ready; evidence is never process/global/persisted cache state.

- [ ] Add red scripted planner cases for 51/101 rows with first-page, complete-chain and standalone-terminal controls; failed middle; guessed token; skipped middle; duplicate; restart; independent scopes; simultaneous independent requests.
- [ ] Implement key admission, pending whole-scope bookkeeping, root reset, successful chain advancement and live-qualified notice. Keep page observation coverage distinct from inventory coverage. Preserve argument-rejection rollback and list_models handling.
- [ ] Add group2 selector controls using canonical identities from preceding identity work: inspection encoding and exact assistance omitted/null/empty semantics; malformed/failed selectors never successfully cover a key.
- [ ] Run scripted loops and selector tests, with exhaustion before terminal page and legacy under50/50 controls. Mutate linkage/reset and terminal-only conditions; every false-completeness mutant must fail.
- [ ] Review and checkpoint planner behavior. Budget exhausted and unfinished traversal remain truthful, without any D7 cap/refusal policy change.

## Task 4 — Browser traversal and all teaching surfaces

Files: `frontend/src/api/client.ts`; new exact blob-page decoder alongside existing operation decoder pattern; `frontend/src/types/api.ts`; `frontend/src/stores/blobStore.ts`; `frontend/src/components/blobs/BlobManager.tsx`; corresponding existing store/component/API tests and a real browser test. Tool descriptions, `toolCallDescriptions.ts`, `docs/reference/composer-tools.md`, both composer skills and `planner_authoring_aids.py`/source-repair pointer sites that currently imply one list is exhaustive.

Interfaces: retain legacy `listBlobs`; add `listBlobPage(sessionId, {limit:50,cursor})`; store retains one traversal's next cursor and exposes `loadMoreBlobs`, with activation epoch and request-sequence ownership. UI explicitly loads more/refreshes, reports loaded count while incomplete and preserves retry state on failed continuation.

- [ ] Write failing exact decoder, load-more/retry/session-switch/refresh/mutation-race tests. Add browser scenario with 51+ real rows that reaches and selects/downloads the oldest via existing controls.
- [ ] Implement one-page-at-a-time store/UI traversal with stale-response fencing and canonical-id deduplication. Invalidate/restart traversal on upload/delete and supersede in-flight continuation on refresh; preserve ordinary operation authority.
- [ ] Update guidance to use explicit paged calls and copied cursors, distinguish all-status versus ready, and state live semantics. Keep unrelated inline/MIME/retention policy untouched. Update contract/teaching snapshots through their generators.
- [ ] Run focused frontend tests, build/typecheck and browser scenario. Assert loaded-count/no-more labels under failure and pending pages, plus mutation-before-response controls.
- [ ] Parent runs applicable whole-tree gates, full default suite and serial PostgreSQL selection on frozen integrated tree, then authorized independent review. Record precise provenance, exit codes, mutation evidence and documented limits before any tracker closure.

## Handoff limits

This brief is usable for planning/review after base reconciliation; it does not assert tests pass, fix findings, or approve merging. Keep nominal DTOs in L0 contracts and codecs in web. The accompanying proposed design fixes wire shape and consistency semantics; an implementer must not silently replace them with always-new roots, offsets, snapshots or an automatic exhaustive tool loop. If review instead requires point-in-time snapshot membership, stop this brief and design that larger authority explicitly with the parent.
