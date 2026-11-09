# `get_blob_content` — bounded, identity-pinned text pages

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for traced/read-test contracts and missing-storage reproduction; High for the size-limit, page-budget, selector-echo and ESTALE findings (reproduced through `execute_tool` with controls, §7 probe runs); Moderate for unmeasured resource impact.**

## 1. Purpose and usage

Use this tool to read selected pages of an authorized UTF-8 artifact, especially when a complete attachment response would exceed the whole-read response limit. For small instruction/reference documents, `read_attachment` gives a stronger complete-delivery contract. Investigate source structure with source inspection rather than treating a text prefix as schema proof. The tool requires the session engine/ID, the storage root, and an exact read-capable session operation authority. Composer MCP does not expose blob tools.

Paging is for spot reads, not for reading a large file whole. The entire decoded file must fit the 262,144-byte credential-inspection limit before any page is returned (§5, get_blob_content-N1). Each page carries only about 3 KB of content on an empty draft (3,045 characters measured), less as standing validation grows, and none at all on a draft with 8 or more dangling transform nodes (get_blob_content-N2). Dependent pages are sequential by contract.

## 2. Contract

Required `blob_id: string`; `offset_chars` is a nonnegative integer defaulting to 0. Optional `expected_content_hash` may be null/omitted at offset 0; otherwise it is a lowercase 64-hex SHA-256. Continuation beyond zero requires a non-null hash; this cross-field rule lives in the [owned argument model](../../../../src/elspeth/web/composer/redaction.py#L3359), not the wire schema, so a violation surfaces as an argument error on both routes. Every supplied hash is checked. The hash argument is `Sensitive` and masked in argument persistence. Character offsets count original Unicode code points, not bytes. Use the returned `next_offset_chars` rather than guessed offsets or dependent parallel requests. `blob_id` must parse as a UUID, but [non-canonical spellings are accepted](../../../../src/elspeth/web/composer/tools/blobs.py#L518) (upper-case, braced, 32-hex, `urn:uuid:`) and are echoed back unchanged (get_blob_content-N3).

Success includes content, blob identity/name/MIME, full byte size, recorded origin (`created_by`, `creation_modality`), verified original-byte hash, full decoded character count, range `[start_char,end_char)`, truncation, and continuation. The handler bounds the serialized envelope **it builds**, checked with `state.validate()`, to [4,096 UTF-8 bytes](../../../../src/elspeth/web/composer/tools/blobs.py#L1967). That envelope includes the session's [complete standing composition validation](../../../../src/elspeth/web/composer/tools/_common.py#L1079). Measured with the handler's own serializer path, a zero-content terminal page is 812 B on an empty draft (362 B of it validation) and 2,144/3,869/6,744 B with 2/5/10 dangling transform nodes. Every nonterminal page also adds the 227-byte `read_attachment_suggestion`. After the handler returns, [`finalize_tool_result`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) [re-normalizes validation](../../../../src/elspeth/web/composer/tools/_common.py#L3814) through the request's catalog/profile view. The delivered envelope is therefore not the one the bound was checked against. Probe runs (§7) showed three things. Under a trained-operator snapshot the two are identical by construction (`validate_authored_composition_state` returns `state.validate()`). Under a web-authored snapshot they differ, and in every differing case the delivered envelope was *smaller*: with 7 nodes naming a plugin outside the catalog the handler refused "cannot fit" against 24 entries while the delivered result carried 8, a false refusal. Whether the delivered envelope can exceed 4,096 bytes stays inconclusive: the branch that could grow it (non-blocking policy evidence appended as warnings) was not exercised. The [50,000-code-point range bound](../../../../src/elspeth/web/composer/redaction.py#L3399) is a response-model invariant and is never the binding limit, since 50,000 code points exceed 4,096 bytes. A terminal empty page at EOF is valid; nonterminal pages must advance.

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

A complete small file returns a terminal page. Larger admissible text returns variable-size pages with an explicit next offset. A replaced artifact rejects continuation under the previous hash. Because decode runs first, a replacement with non-UTF-8 bytes reports an encoding error rather than "content changed". Credentials after or spanning a page boundary refuse the first page. A decoded file over 262,144 bytes refuses every page as a credential detection. A draft with enough standing validation entries refuses every page with "Blob page cannot fit the bounded tool response". Non-UTF-8 content fails safely; this inspection tool has no encoding selector even though runtime inline references support several encodings. Pending/error states refuse before disclosure. Hash corruption escalates through the audited crash path. An NFS `ESTALE` on read is not caught and escapes `execute_tool` as an untyped `OSError` (get_blob_content-N6, demonstrated by injected errno). An interrupted blob replacement/deletion is reconciled before reading a committed version.

## 5. Usability assessment

**BOS-02 — demonstrated failure-taxonomy divergence, High confidence:** for ready owned metadata with a missing backing file, this tool returns ordinary `success=false`, while `BlobServiceImpl.read_blob_content_sync` raises `BlobContentMissingError` under the same operation fence. The local probe reproduced both paths. No bytes leak, but the model receives a repairable-looking tool failure for an infrastructure/integrity problem it cannot fix. Operator storage recovery is the workaround; repeated argument edits are inappropriate.

**BOS-05 — source-established resource limitation:** each small page uses a complete `Path.read_bytes()`, a whole-file hash/decode, and a whole-content credential pass. On top of that comes a binary search of up to about 16 response admissions and envelope serializations, each over the whole standing validation. The response bound does not bound the work or the peak retained bytes, and repeated pages repeat all of it. No latency/memory magnitude was measured. The whole-input credential restriction itself is intended and must remain.

**get_blob_content-N1 — size limit reported as a credential; demonstrated through `execute_tool` with ±1 and credential controls (§7 probe 1), High confidence:** the [handler](../../../../src/elspeth/web/composer/tools/blobs.py#L2084) passes the full decoded payload to [`_credential_safe_blob_discovery_result`](../../../../src/elspeth/web/composer/tools/blobs.py#L121). That helper catches `CredentialMaterialRefused` and discards its category. Introspection controls: a payload with 262,144 `x` characters returns no finding; 262,145 returns `traversal_limit`, and the helper returns `credential_material_rejected` with "This control content appears to contain a credential. Store the value through the secret service and use a secret reference." A benign large file therefore draws wrong advice. Through `execute_tool`, a benign 262,145-byte blob and a 952-byte blob holding a real `sk-` key return the same code and the same message, so the model cannot tell them apart; 262,144 bytes returns a page. The tool does not disclose the limit. No get_blob_content test references the limit or uses a payload above it: the largest literal payloads in `test_tools_blobs.py`/`test_tools.py` are about 50,000 characters, found by a grep for numeric literals of six or more digits.

**get_blob_content-N2 — standing validation consumes the page budget; demonstrated through `execute_tool` (§7 probe 2), High confidence:** the bounded envelope carries the full current validation (errors, warnings, suggestions, semantic contracts, repair suggestions). With 10 dangling nodes (32 errors), even an empty page serializes to 6,744 B, so no page of any blob can be returned and the refusal names no cause. Through `execute_tool`, a 31-byte blob reads on drafts with 0/2/5 dangling nodes (846/2,178/3,903 B delivered) and refuses "Blob page cannot fit the bounded tool response" with `error_code` null at 8, 9 and 10 nodes (probe 2). That sweep skipped 6 and 7. The trained-snapshot sweep of probe 2b, through the same `execute_tool` helper and the same dangling-node states, read at 1–5 nodes (3,328 B at 4, 3,903 B at 5) and refused at 6 and 7 nodes (21 and 24 errors). The measured onset is therefore 6 dangling nodes (21 errors), not the 8 that the probe-wave record first stated from probe 2 alone. The model cannot repair this with tool arguments. Under a web-authored snapshot the bound is checked against pre-normalization validation, which produced a false refusal (§2). The magnitude in real drafts was not measured.

**get_blob_content-N3 — non-canonical selector echoed; echo and coverage split demonstrated (§7 probe 3), advisor-recovery and planner-key effects source-established:** the lookup canonicalizes but `data.blob_id` echoes the raw spelling. The same blob read under two spellings forms two coverage series (never `complete`): reading a 5,000-character blob's first page by canonical id and its second by upper-case id gave two series, `[0,3049)` and `[3049,5000)`, neither complete, while the canonical-only control gave one complete series. By source, the raw echo also defeats advisor read recovery's id comparison and produces distinct planner information keys; neither was probed. This extends BOS-03 from lookup to echoed identity.

**get_blob_content-N4 — claimed parity gate does not exist; source-established:** the handler docstring says drift from `BlobServiceImpl.read_blob_content` "is caught by `TestGetBlobContentGuards` at CI time". That [class](../../../../tests/unit/web/composer/test_tools_blobs.py#L2546) mentions the service only in its docstring and has no missing-file case. Instruments: `grep -rn "storage file missing" src tests` returns 4 source hits and 0 test hits. Among composer unit test files that both call `get_blob_content` and contain `unlink`/`FileNotFoundError`, the hits are a `delete_blob` missing-file test, a preview test, and crash-reconciliation cases that restore storage before reading. BOS-02 is exactly the drift such a gate would catch. The docstring's "canonical path is async, so guards are mirrored inline" is stale: [`read_blob_content_sync`](../../../../src/elspeth/web/blobs/service.py#L3327) exists, and the tool already uses the service's private custody context.

**get_blob_content-N5 / N6 / N8 — partial guard reconstruction; N6 demonstrated by injected errno (§7 probe 4), N5 observed on two branches, N8 source-established:** failure branches have no `error_code`, whereas `inspect_source` uses `source_blob_unavailable` for the same missing file; the probes observed `error_code` null on the storage-missing and "cannot fit" branches. Only ENOENT is softened, while the service types [ENOENT and ESTALE](../../../../src/elspeth/web/blobs/service.py#L165) as [missing content](../../../../src/elspeth/web/blobs/service.py#L2454). With `Path.read_bytes` patched to raise for the blob's storage path only, ESTALE escaped `execute_tool` as `OSError` (errno 116) while `read_blob_content_sync` raised `BlobContentMissingError`; the ENOENT control gave the tool's failed result against the service's `BlobContentMissingError`, and the EIO control propagated `OSError` from both. What the freeform batch or planner does with the escaped `OSError` was not probed. The service's [operation-kind allowlist](../../../../src/elspeth/web/blobs/service.py#L144) is not applied. Whether a disallowed operation kind or a real ESTALE reaches this tool in practice is unverified; ESTALE is plausible on the NFS-backed container targets.

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

The sheet review added read-only introspection against this worktree. The credential detector gave `None` at 262,144 bytes and `traversal_limit` at 262,145, and the helper turned the latter into `credential_material_rejected`. Envelope sizes through `_discovery_result` → `admit_discovery_result` → `serialize_admitted_discovery_result` were 812/2,144/3,869/6,744 B for 0/2/5/10 dangling nodes. `_blob_id_uuid_validation_error` accepted upper-case, braced and 32-hex forms. No test covers the missing-file branch, the size limit, a non-trivial validation envelope, ESTALE, or non-canonical selectors.

### 7.1 Probe runs (2026-11-09)

Each probe ran in memory (SQLite session store, real blob bytes with their real hash, a genuine fenced operation context) through the test shim's `execute_tool`, from the worktree root with:

```bash
cd $WT && PROBE_L=$L PROBE_WT=$WT TMPDIR=$L/tmp PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src \
  <main-checkout>/.venv/bin/python $L/<probe>.py > $L/<probe>.log 2>&1; echo "exit=$?"
```

`$L` is the lane-private probe directory; scripts, stores and logs live there, not in the tree. Every run printed `elspeth.__file__` inside `$WT/src`. The scripts print verdicts rather than asserting them, so every exit code is 0 by design; the outcome is read from the observed values below.

| # | Finding | Script | Exit | Observed | Outcome |
|---|---|---|---|---|---|
| 1 | N1 | `probe_n1_size_limit.py` | 0 | Benign 262,145 B → `success=false`, `credential_material_rejected`, "appears to contain a credential… secret reference", `data=null`. Control 262,144 B → page `[0,3045)`, `truncated`, next 3045. Known-positive control, 952 B with `\nsk-` + 48 `A` → the same code and message | **confirmed** |
| 2 | N2 | `probe_n2_validation_budget.py` | 0 | 31-byte blob, trained snapshot. 0/2/5 dangling nodes (2/8/17 entries) → page, 846/2,178/3,903 B delivered. 8/9/10 nodes (26/29/32 entries) → "Blob page cannot fit the bounded tool response; no content was returned.", `error_code` null. Control: empty draft succeeds. This sweep skipped 6 and 7; probe 2b's trained rows (same helper and states) refuse at 6 (21 entries), so the onset is 6 nodes | **confirmed** (onset 6 nodes, from this row with 2b) |
| 2b | §2 post-normalization | `probe_n2b_post_normalization.py` | 0 | Trained snapshot, `passthrough` and out-of-catalog plugin, 1–7 nodes: handler and delivered validation and bytes identical in every row, as `validate_authored_composition_state` requires for a trained operator. Rows 1–5 read a page; rows 6 and 7 refused "cannot fit" for both plugins (21 and 24 entries), which places N2's onset at 6 (`$L/n2b.log`) | **inconclusive** (non-discriminating by construction) |
| 2c | §2 post-normalization | `probe_n2c_web_authored.py` | 0 | Web-authored snapshot. `passthrough` rows identical. Out-of-catalog plugin rows differ, delivered always smaller: 3 nodes 2,753 → 1,121 B, 5 nodes 3,903 → 1,463 B (17 → 5 entries); 7 nodes the handler refused "cannot fit" on 24 entries while the delivered result carried 8. No successful page exceeded 4,096 B; warnings were 0 in every row | **confirmed** that the checked and delivered envelopes differ and that a false refusal occurs; **inconclusive** for overshoot past 4,096 B (the warning-append branch was not exercised) |
| 3 | N3 | `probe_n3_selector_echo.py` | 0 | 5,000-char blob. Page 2 read by `id.upper()` echoes the upper-case id; `TurnEvidence` delivery gives two series, `[0,3049)` and `[3049,5000)`, neither `complete`. Control: canonical id for both pages → one series `[0,5000)`, `complete` | **confirmed** (echo and coverage split; advisor recovery and planner key not probed) |
| 4 | N6 | `probe_n6_estale.py` | 0 | `Path.read_bytes` patched for the storage path only. ESTALE: tool raises `OSError` errno 116, service raises `BlobContentMissingError`. Controls: no patch → both succeed; ENOENT → tool failed result "Blob storage file missing…" (`error_code` null), service `BlobContentMissingError`; EIO → both raise `OSError` | **confirmed** (injected errno; real NFS reachability and the batch/planner handling of the escaped exception not probed) |

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
