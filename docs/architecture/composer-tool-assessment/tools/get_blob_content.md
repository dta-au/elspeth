# `get_blob_content` — bounded, identity-pinned text pages

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for traced/read-test contracts and missing-storage reproduction; Moderate-High for the size-limit and page-budget findings (measured at the shared helper/serializer, not end-to-end); Moderate for unmeasured resource impact.**

## 1. Purpose and usage

Use this tool to read selected pages of an authorized UTF-8 artifact, especially when a complete attachment response would exceed the whole-read response limit. For small instruction/reference documents, `read_attachment` gives a stronger complete-delivery contract. Investigate source structure with source inspection rather than treating a text prefix as schema proof. The tool requires the session engine/ID, the storage root, and an exact read-capable session operation authority. Composer MCP does not expose blob tools.

Paging is for spot reads, not for reading a large file whole. The entire decoded file must fit the 262,144-byte credential-inspection limit before any page is returned (§5, get_blob_content-N1). Each page carries only about 3 KB of content on an empty draft, less as standing validation grows (get_blob_content-N2). Dependent pages are sequential by contract.

## 2. Contract

Required `blob_id: string`; `offset_chars` is a nonnegative integer defaulting to 0. Optional `expected_content_hash` may be null/omitted at offset 0; otherwise it is a lowercase 64-hex SHA-256. Continuation beyond zero requires a non-null hash; this cross-field rule lives in the [owned argument model](../../../../src/elspeth/web/composer/redaction.py#L3359), not the wire schema, so a violation surfaces as an argument error on both routes. Every supplied hash is checked. The hash argument is `Sensitive` and masked in argument persistence. Character offsets count original Unicode code points, not bytes. Use the returned `next_offset_chars` rather than guessed offsets or dependent parallel requests. `blob_id` must parse as a UUID, but [non-canonical spellings are accepted](../../../../src/elspeth/web/composer/tools/blobs.py#L518) (upper-case, braced, 32-hex, `urn:uuid:`) and are echoed back unchanged (get_blob_content-N3).

Success includes content, blob identity/name/MIME, full byte size, recorded origin (`created_by`, `creation_modality`), verified original-byte hash, full decoded character count, range `[start_char,end_char)`, truncation, and continuation. The handler bounds the serialized envelope **it builds**, checked with `state.validate()`, to [4,096 UTF-8 bytes](../../../../src/elspeth/web/composer/tools/blobs.py#L1967). That envelope includes the session's [complete standing composition validation](../../../../src/elspeth/web/composer/tools/_common.py#L1079). Measured with the handler's own serializer path, a zero-content terminal page is 812 B on an empty draft (362 B of it validation) and 2,144/3,869/6,744 B with 2/5/10 dangling transform nodes. Every nonterminal page also adds the 227-byte `read_attachment_suggestion`. After the handler returns, [`finalize_tool_result`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) [re-normalizes validation](../../../../src/elspeth/web/composer/tools/_common.py#L3814) through the request's catalog/profile view. The delivered envelope is therefore not the one the bound was checked against; whether it can exceed 4,096 bytes is unverified. The [50,000-code-point range bound](../../../../src/elspeth/web/composer/redaction.py#L3399) is a response-model invariant and is never the binding limit, since 50,000 code points exceed 4,096 bytes. A terminal empty page at EOF is valid; nonterminal pages must advance.

Before any page is disclosed, the complete decoded artifact is [credential-inspected](../../../../src/elspeth/web/composer/tools/blobs.py#L2084). Paging therefore cannot bypass whole-input inspection, and credentials beyond a page boundary still refuse the first page. The inspection's [string limit is 262,144 UTF-8 bytes](../../../../src/elspeth/contracts/credential_material.py#L132). A larger file yields `traversal_limit`, which the [blob discovery helper](../../../../src/elspeth/web/composer/tools/blobs.py#L121) reports as `credential_material_rejected` with the [credential refusal text](../../../../src/elspeth/contracts/credential_material.py#L24) ("appears to contain a credential… use a secret reference"). `read_attachment` instead [pre-checks size](../../../../src/elspeth/web/composer/tools/blobs.py#L2200) with an honest message.

It is uncached `BLOB_DISCOVERY` on the freeform loop, does not change composition version, and needs no mutation approval. Metadata/read/hash run under the shared custody lock. Malformed arguments are safe argument errors. Missing/not-ready blobs, bad encoding, offset, expected-hash mismatch, and disclosure-size refusals return failure without content. Only the credential refusal [carries an `error_code`](../../../../src/elspeth/web/composer/tools/blobs.py#L2029); the other branches are prose-only (get_blob_content-N5). Hash corruption and a null ready hash escalate. The current helper turns a missing backing file ([`FileNotFoundError` only](../../../../src/elspeth/web/composer/tools/blobs.py#L1957)) into a failed result instead of an integrity exception. Tool content is replaced by a fixed sentinel in persisted tool-result projections; provider delivery still treats it as untrusted evidence. Repeating a pinned page is safe but performs a new full read/inspection.

## 3. Implementation trace

[Handler and declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L1970) use the strict [argument/range models](../../../../src/elspeth/web/composer/redaction.py#L3328). The order of checks:

1. Session context.
2. Argument model.
3. UUID parse.
4. [`_locked_read_ready_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L1930), which requires `data_dir`, an exact `SessionOperationContext` plus authority, and a fence session match. Unlike the service, it does not check the operation-kind allowlist (get_blob_content-N8). It then enters the [shared custody/recovery context](../../../../src/elspeth/web/blobs/service.py#L3259), which looks the row up by canonical `UUID`, reconciles crash leftovers, reads bytes with `Path.read_bytes()`, and verifies the hash.
5. Status, missing bytes, UTF-8 decode, and the expected-hash compare, in that order.
6. Offset range.
7. Whole-content credential inspection.
8. A binary search over `end_char` (at most about 16 admissions/serializations) using the actual selected response serializer.

[Response admission](../../../../src/elspeth/web/composer/tools/blobs.py#L245) closes range/origin shape. It narrows `created_by` to `BlobCreator`, checks `creation_modality` against `CreationModality`, and requires `len(content) == end_char - start_char`. The payload echoes the caller's selector as `blob_id` ([source](../../../../src/elspeth/web/composer/tools/blobs.py#L2065)), not the row's canonical `id`.

Registration/exposure use the [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L94) ([response contract lookup](../../../../src/elspeth/web/composer/tools/_registry.py#L108)) and [canonical definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), followed by provider wire projection. [Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) and [worker execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008) supply context and [admit discovery data](../../../../src/elspeth/web/composer/tool_batch.py#L3052) before audit/disclosure. [Content-part construction](../../../../src/elspeth/web/composer/discovery_cache.py#L59) renders the page as default-untrusted parts; it is not an `AdmittedAttachmentResponse` and is not reference-trusted. [Audit finalization](../../../../src/elspeth/web/composer/audit.py#L812) records dispatch and semantic outcomes separately. [Frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) consume server-derived outcomes.

### 3.1 Route inventory

| Route | How it reaches / consumes the tool | Gates |
|---|---|---|
| Canonical registry/emitter | `TOOLS_IN_MODULE` ([blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2291)) → `_REGISTERED_TOOLS` → `get_tool_definitions` | Always declared; `kind=BLOB_DISCOVERY`; selected response contract |
| Freeform loop | [`composer_loop_tool_definitions`](../../../../src/elspeth/web/composer/provider_gateway.py#L391) sends the whole wire projection. `tool_batch` calls `execute_tool` with `validate_arguments`, `raise_schema_argument_errors` and `require_data_dir_for_paths=True`. It then admits, [registers the page with turn evidence](../../../../src/elspeth/web/composer/tool_batch.py#L3348), and [skips the discovery cache](../../../../src/elspeth/web/composer/tool_batch.py#L3368) | Custody `data_dir`/engine/operation context/authority from the turn context; per-turn tool-call limit (default 16) and discovery-turn budget ([config](../../../../src/elspeth/web/config.py#L369)) |
| Strict wire dialect | `openai_strict` promotes `offset_chars`/`expected_content_hash` to required-nullable and moves `default` into description text ([pinned](../../../../tests/unit/web/composer/test_strict_profile.py#L362)); decode strips the nulls | Dialect per model config |
| Planner | Advertised by [`PLANNER_DISCOVERY_TOOL_NAMES`](../../../../src/elspeth/web/composer/capability_skill.py#L29). Dispatched through [`execute_discovery_tool_with_context`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L999) with a [frozen request context](../../../../src/elspeth/web/composer/pipeline_planner.py#L3422). Records the information key [`blob.content:(id, offset, hash)`](../../../../src/elspeth/web/composer/pipeline_planner.py#L447); a failure marks the key unavailable. Captures a `VisibleToolEvent` and [registers the page](../../../../src/elspeth/web/composer/pipeline_planner.py#L5028). Independent calls run as parallel tasks; a raised integrity error cancels siblings | `PlannerCustodyConfig.session_operation_context` is Optional ([config](../../../../src/elspeth/web/composer/pipeline_planner.py#L1158), [construction](../../../../src/elspeth/web/composer/planning_application.py#L1097)); None would raise `AuditIntegrityError` rather than return a failed result (get_blob_content-N10, hypothesis) |
| Advisor historical read recovery | [`read_advisor_history_blob`](../../../../src/elspeth/web/composer/service.py#L1211) wraps `_locked_read_ready_blob` for [`_recover_read_bodies`](../../../../src/elspeth/web/composer/advisor_transcript.py#L488). That function re-reads the whole file, re-runs credential inspection at the default 262,144-byte policy, and re-slices recorded `get_blob_content` ranges for the advisor transcript | Requires engine, session ID and operation context; a mismatched echoed `blob_id` yields `unavailable_stale_or_mismatched_blob` ([compare](../../../../src/elspeth/web/composer/advisor_transcript.py#L531)) |
| Turn evidence / publication review | [`register_blob_page`](../../../../src/elspeth/web/composer/turn_effects.py#L396) → [`_coverage`](../../../../src/elspeth/web/composer/turn_effects.py#L219) merges ranges per `(blob_id, content_hash, total_chars)`; `complete` reaches the [advisor publication review](../../../../src/elspeth/web/composer/turn_effects.py#L703) | Successful admitted results only; series keyed on the echoed selector |
| Recovery controls | None. `recovery_controls.py` has 0 mentions (control: `restore_parked_graph` = 4) | — |
| Composer MCP | Excluded by construction: [`_COMPOSER_TOOL_NAMES`](../../../../src/elspeth/composer_mcp/server.py#L117) is discovery ∪ mutation only | — |
| Proposal/approval, async | None. A discovery tool does not create proposal rows; there is no async/operation variant | — |
| REST / frontend | No direct REST route. Persisted rows show the content sentinel; GET /messages stamps outcomes; the card uses the [read-only description](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L44). Sibling HTTP readers ([download/preview](../../../../src/elspeth/web/blobs/routes.py#L402)) use the service directly and are not routes of this tool | Session auth |

No tutorial-specific branch was found for this tool.

## 4. Alternative paths

A complete small file returns a terminal page. Larger admissible text returns variable-size pages with an explicit next offset. A replaced artifact rejects continuation under the previous hash. Because decode runs first, a replacement with non-UTF-8 bytes reports an encoding error rather than "content changed". Credentials after or spanning a page boundary refuse the first page. A decoded file over 262,144 bytes refuses every page as a credential detection. A draft with enough standing validation entries refuses every page with "Blob page cannot fit the bounded tool response". Non-UTF-8 content fails safely; this inspection tool has no encoding selector even though runtime inline references support several encodings. Pending/error states refuse before disclosure. Hash corruption escalates through the audited crash path. An NFS `ESTALE` on read is not caught and would escape as an untyped `OSError` (get_blob_content-N6). An interrupted blob replacement/deletion is reconciled before reading a committed version.

## 5. Usability assessment

**BOS-02 — demonstrated failure-taxonomy divergence, High confidence:** for ready owned metadata with a missing backing file, this tool returns ordinary `success=false`, while `BlobServiceImpl.read_blob_content_sync` raises `BlobContentMissingError` under the same operation fence. The local probe reproduced both paths. No bytes leak, but the model receives a repairable-looking tool failure for an infrastructure/integrity problem it cannot fix. Operator storage recovery is the workaround; repeated argument edits are inappropriate.

**BOS-05 — source-established resource limitation:** each small page uses a complete `Path.read_bytes()`, a whole-file hash/decode, and a whole-content credential pass. On top of that comes a binary search of up to about 16 response admissions and envelope serializations, each over the whole standing validation. The response bound does not bound the work or the peak retained bytes, and repeated pages repeat all of it. No latency/memory magnitude was measured. The whole-input credential restriction itself is intended and must remain.

**get_blob_content-N1 — size limit reported as a credential; demonstrated at the shared helper, handler reach source-established, High confidence:** the [handler](../../../../src/elspeth/web/composer/tools/blobs.py#L2084) passes the full decoded payload to [`_credential_safe_blob_discovery_result`](../../../../src/elspeth/web/composer/tools/blobs.py#L121). That helper catches `CredentialMaterialRefused` and discards its category. Introspection controls: a payload with 262,144 `x` characters returns no finding; 262,145 returns `traversal_limit`, and the helper returns `credential_material_rejected` with "This control content appears to contain a credential. Store the value through the secret service and use a secret reference." A benign large file therefore draws wrong advice. The tool does not disclose the limit. No get_blob_content test references the limit or uses a payload above it: the largest literal payloads in `test_tools_blobs.py`/`test_tools.py` are about 50,000 characters, found by a grep for numeric literals of six or more digits.

**get_blob_content-N2 — standing validation consumes the page budget; demonstrated at the serializer, Moderate-High confidence:** the bounded envelope carries the full current validation (errors, warnings, suggestions, semantic contracts, repair suggestions). With 10 dangling nodes (32 errors), even an empty page serializes to 6,744 B, so no page of any blob can be returned and the refusal names no cause. The model cannot repair this with tool arguments. The magnitude in real drafts and the post-normalization delivered size were not measured.

**get_blob_content-N3 — non-canonical selector echoed; source-established:** the lookup canonicalizes but `data.blob_id` echoes the raw spelling. The same blob read under two spellings forms two coverage series (never `complete`), defeats advisor read recovery's id comparison, and produces distinct planner information keys. This extends BOS-03 from lookup to echoed identity.

**get_blob_content-N4 — claimed parity gate does not exist; source-established:** the handler docstring says drift from `BlobServiceImpl.read_blob_content` "is caught by `TestGetBlobContentGuards` at CI time". That [class](../../../../tests/unit/web/composer/test_tools_blobs.py#L2546) mentions the service only in its docstring and has no missing-file case. Instruments: `grep -rn "storage file missing" src tests` returns 4 source hits and 0 test hits. Among composer unit test files that both call `get_blob_content` and contain `unlink`/`FileNotFoundError`, the hits are a `delete_blob` missing-file test, a preview test, and crash-reconciliation cases that restore storage before reading. BOS-02 is exactly the drift such a gate would catch. The docstring's "canonical path is async, so guards are mirrored inline" is stale: [`read_blob_content_sync`](../../../../src/elspeth/web/blobs/service.py#L3327) exists, and the tool already uses the service's private custody context.

**get_blob_content-N5 / N6 / N8 — partial guard reconstruction; source-established:** failure branches have no `error_code`, whereas `inspect_source` uses `source_blob_unavailable` for the same missing file. Only ENOENT is softened, while the service types [ENOENT and ESTALE](../../../../src/elspeth/web/blobs/service.py#L165) as [missing content](../../../../src/elspeth/web/blobs/service.py#L2454). The service's [operation-kind allowlist](../../../../src/elspeth/web/blobs/service.py#L144) is not applied. Whether a disallowed operation kind or ESTALE reaches this tool in practice is unverified; ESTALE is plausible on the NFS-backed container targets.

**get_blob_content-N7 — unfollowable recovery instruction; source-established, low:** the hash-mismatch refusal says "restart inspection with its current content hash" but returns no data, and the skill forbids recomputing hashes. The model has to infer that it should re-read at offset 0 with a null hash.

**get_blob_content-N10 — planner optional custody context; hypothesis:** see §3.1. No production caller passing None was traced.

**get_blob_content-N11 — whole-file paging is impractical; hypothesis (arithmetic):** at about 3 KB of content per page, one dependent page per provider round-trip, and the example deployments' 10 discovery turns, a file near the 262,144-byte limit needs about 85+ sequential calls. Turn accounting was not traced in detail.

### 5.1 Guidance parity

Instrument: `grep -n "get_blob_content\|read_attachment\|next_offset\|offset_chars\|expected_content_hash\|inspect_source"` over both skills. It matches pipeline_composer.md at 344, 381-418 and 662-700, and gets 0 matches in pipeline_capabilities.md. That zero is controlled by `list_sources` = 2 matches in the same file. Both skills reach both routes. The service renders [`render_system_prompt`](../../../../src/elspeth/web/composer/prompts.py#L78) once (the capability core, then the composer overlay, then the optional deployment skill) and passes that same text to the planner as `rendered_skill`.

- **Missing limit, wrong next action.** The [declaration description](../../../../src/elspeth/web/composer/tools/blobs.py#L2141) says nothing about the whole-file inspection limit. [pipeline_composer.md](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L388) says paging "does not bypass a whole-input inspection refusal" but gives no number and does not say the refusal will read as a credential detection. The refusal text then sends the model to the secret service, which is the wrong action (N1). Only `read_attachment`'s own description mentions "the same credential inspection limit".
- **Undisclosed page-budget dependency.** Neither the description nor either skill says that standing validation shrinks, or entirely removes, page capacity. "If only the complete response envelope cannot fit, use `get_blob_content` for selected pages" ([skill](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L388)) can route the model to a tool that also cannot fit on an invalid draft (N2).
- **No mismatch recovery.** The skill teaches "the same content hash" for continuation ([L412-L418](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L412)) and "Do not manually recompute hashes" ([L385](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L385)), but nothing says how to recover from a refused continuation (N7).
- **Ambiguous wording.** The description's "`truncated` means end_char < total_chars, not whole-file inspection" is easy to misread: the whole file *is* inspected for credentials; what `truncated` means is that the whole file was not delivered.
- **Consistent.** Sequential, non-guessed paging and per-blob dependency agree across the description, pipeline_composer.md and [pipeline_capabilities.md](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L44). The preference for `read_attachment` and `inspect_source` matches the handler. No stale tool/argument references or inter-skill contradictions were found. The capability core does not need tool-specific text.
- **UI copy.** The [frontend description](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L44), "Reads the text content of a session file", implies a whole-file read for a page read (a mild case of the GS-03 class).

## 6. Structural remediation

Use one owned verified-content reader for API, tools, source inspection, preview and advisor read recovery, with consistent lifecycle/integrity exceptions (including ESTALE and the operation-kind allowlist), canonical identity echo and structured actionability. Share snapshot/identity facts and resource policy. Do not weaken full-artifact credential admission to inspect only disclosed pages, but report its traversal/size limit as its own category. For admissible text, reuse a request-scoped verified identity/content admission or a bounded streaming design where practical, invalidated by replacement and policy changes. Decide whether a page-bounded discovery response should carry the whole standing validation at all; the handler and the delivered envelope should at least be measured by the same validator. Page-coverage evidence already exists: turn evidence merges ranges and reports `complete` to the advisor publication review. It needs a canonical identity key, and the orchestrator could use it beyond that review.

Acceptance:

- Exact Unicode reconstruction, byte-bounded serializer output, progressing offsets, and expected-hash refusal remain.
- Corrupt/missing owned bytes, ENOENT and ESTALE alike, get the same nonrepairable classification on every route.
- Credentials anywhere in the artifact refuse every page; an oversized benign file is refused as oversized, not as a credential.
- Equivalent UUID spellings yield one canonical echoed identity and one coverage series.
- The page budget is either independent of standing validation or the refusal says that it is not.
- Resource work is explicitly bounded/measured.
- Cross-session/stale authority fails closed.

Compatibility risk is Medium for failure categories/caching and the validation envelope, and Low for preserving the current page schema. A maximum-input warning is temporary containment, not the shared-reader solution.

### 6.1 Simple enhancements

| What | Where | Why | Acceptance check |
|---|---|---|---|
| Refuse a file whose decoded size exceeds `READ_ATTACHMENT_MAX_UTF8_BYTES` before credential inspection, with a size message and code; or map `traversal_limit` to a size refusal in the helper | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2084), [helper](../../../../src/elspeth/web/composer/tools/blobs.py#L121) | N1: wrong cause, wrong next action | 262,145-byte benign blob → non-credential code/message; 262,144 → page; a credential inside a 1 KB file still → `credential_material_rejected` |
| Give "Blob page cannot fit" an `error_code` and say that standing validation/metadata takes up the budget | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2133) | N2 actionability | 10-dangling-node state + small blob → coded refusal naming the envelope; empty state → page |
| Echo `blob["id"]` instead of the raw selector (also in `read_attachment`) | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2065) | N3 | Upper-case selector → canonical `data.blob_id`; two spellings → one coverage series |
| Add error codes for not found / not ready / storage missing / not UTF-8 / content changed / offset out of range, registered in the emitted-code set | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2029) | N5 | Each branch's `errors[0].error_code` is pinned by a test |
| Change the mismatch text to "re-read at `offset_chars` 0 with `expected_content_hash` null"; optionally compare the expected hash before decode | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2058) | N7 | Replaced non-UTF-8 blob under the old hash → "content changed" |
| Treat ESTALE like ENOENT, or reuse the service's `_read_bytes_or_missing` | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L1957) | N6 | Monkeypatched ESTALE → the same classification as ENOENT |
| Correct the handler/test docstrings; add a missing-file test pinning the current branch (or the BOS-02 decision) | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L1975), [tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L2546) | N4 | A test fails if the missing-file branch changes without a decision |
| Disclose the 262,144-byte limit and the envelope-inclusive budget in the description; reword `truncated` | [declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L2141) | §5.1 | Description/skill teaching tests updated; wire-limits gate still passes |
| Frontend: "Reads a page of a session file's text for inspection." | [toolCallDescriptions.ts](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L44) | §5.1 | Frontend description test updated |
| Replace the "length-only summary" claims in sibling redaction reasons with "fixed sentinel" | [redaction.py](../../../../src/elspeth/web/composer/redaction.py#L2967), [L3001](../../../../src/elspeth/web/composer/redaction.py#L3001) | N9: `_summarize_blob_content` emits no length | Redaction policy snapshot regenerated |
| Remove the `verified_hash is None` check, which can never run because `_verify_blob_content_hash` already raised | [blobs.py](../../../../src/elspeth/web/composer/tools/blobs.py#L2055) | Dead branch | Null-hash test still raises `AuditIntegrityError` |

## 7. Evidence, gaps, and caveats

Read [exact Unicode/serialization tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L2687), [the empty-state envelope bound](../../../../tests/unit/web/composer/test_tools_blobs.py#L2843), [credential seam cases](../../../../tests/unit/web/composer/test_tools_blobs.py#L2824), the replaced-blob refusal, hash/encoding failures, and the [mixed-version read proof](../../../../tests/unit/web/composer/test_tools_blobs.py#L4013). A PostgreSQL custody-race test exists with `get_blob_content` as a [contender](../../../../tests/testcontainer/web/test_blob_read_custody_race.py#L137); it was not run. The seven-file focused run exited 0: **448 passed, 43 warnings, 66.95s**. A controlled SQLite probe demonstrated the missing-file divergence.

The sheet review added read-only introspection against this worktree. The credential detector gave `None` at 262,144 bytes and `traversal_limit` at 262,145, and the helper turned the latter into `credential_material_rejected`. Envelope sizes through `_discovery_result` → `admit_discovery_result` → `serialize_admitted_discovery_result` were 812/2,144/3,869/6,744 B for 0/2/5/10 dangling nodes. `_blob_id_uuid_validation_error` accepted upper-case, braced and 32-hex forms. None of the new findings was reproduced through `execute_tool` end-to-end; the exact probes are recorded with the lane report. No test covers the missing-file branch, the size limit, a non-trivial validation envelope, ESTALE, or non-canonical selectors.

No live provider, PostgreSQL, browser, memory profile, or throughput measurement ran. Source-level full reads establish the shape of the work, not its measured operational severity. Nothing in this sheet has been implemented.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_get_blob_content`](../../../../src/elspeth/web/composer/tools/blobs.py#L1970).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "blob_id": {
      "type": "string",
      "description": "ID of the blob to read."
    },
    "offset_chars": {
      "type": "integer",
      "minimum": 0,
      "default": 0,
      "description": "Start offset in original Unicode code points."
    },
    "expected_content_hash": {
      "type": [
        "string",
        "null"
      ],
      "pattern": "^[0-9a-f]{64}$",
      "description": "Verified original-byte SHA256. Use null at offset 0 for no expected identity constraint; a canonical non-null hash is required after offset 0. Every non-null hash is checked, even at offset 0."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
