# Next bounded remediation candidates after D1/D2

Read-only triage, 9 November 2026. Main HEAD verified with `git rev-parse HEAD`: `5e192342c3fb025fd9a91244bebe437158e9de8e`. Read assessment README/register, R1/R2/R4 remediation scope, individual blob/list_sinks sheets, lane progress/recon, and live [canonical tracker](https://chatgpt.com/space/page_22de36ba40a08191b1aca9ae0b943d60) sequence9 (read only; updated11:11:52 UTC). No code edits, tests, reproduction runs, agents, tracker mutations or closure claims. Findings below are **current source-established**, backed by the audit's dated demonstrations; they require new red/green behavior evidence on the final D1/D2 integration base.

Integrated work to exclude: Q1/02.03 secret acceptance policy (`b8921de06`), GS-02/12.07 matched diagnostic codes (`f598aa43d`), Q25/13.16 exact grammar-token test (`5e192342c`). Local commit `66739807c9acbdae6f2d7d9f1de858b8e69c9793` changes only legacy proposal acceptance, sessionStore and their tests: it closes neither identity nor discovery findings. Tracker02.11/02.12 correctly remain open for canonical/final D1 integration. Do not duplicate that work or D2 path/lifecycle02.17. Implement candidates serially after D1/D2 since tool_batch, tools/blobs and planner files are shared.

## Recommended order

| Order | Bounded group | Audit / tracker | Why ready |
| --- | --- | --- | --- |
| 1 | Canonical incoming resource identity, including retention | BOS-03, XC-08, identity part XC-13;03.01-03.05 | Existing UUID admission already accepts equivalent spellings; normalize that admitted value instead of reporting false absence or checking retention against a different spelling. No new resource policy. |
| 2 | Honest bounded inventories and discriminating planner observation keys | BOS-01/XC-13;03.09,03.12-03.15; supporting15.04/12.12 | Stop claiming a bounded window is complete; key observations by actual admitted selectors. Stable tie ordering and honest disclosure can land without deciding a new pagination wire API. |
| 3 | Cache-hit evidence records actual delivered result | XC-25, list_sinks-N5;11.15 | The model already receives the correct rebuilt envelope; persist that same admitted result rather than the slim surrogate. No product behavior/budget change. |

These groups have no unresolved D1-D38 product ruling within the stated boundary. Full pagination beyond group2 needs a concrete compatibility/concurrency design, described below; it must not be marked done by the containment slice.

## 1. Canonical resource identity and retention

### Current source evidence

- `composer/tools/blobs.py::_blob_id_uuid_validation_error`518 calls `UUID(blob_id)` only to validate; returns None for upper/mixed case, braces, hyphenless and urn forms without returning the canonical representation. `_sync_get_blob`499 queries raw text.
- `_handle_get_blob_metadata`660 validates `validated.blob_id` but sends the raw string to `_sync_get_blob`671. `tools/sources.py::_execute_set_source_from_blob` path799-845 does the same; plural binding1340-1405 uses `.lower()` for duplicate detection but raw id lookup. Lowercasing does not unify braces/hyphenless/urn.
- `blobs.py::_execute_update_blob`1700+, `_execute_delete_blob`1808+ use UUID for actual custody operations but retain raw `validated.blob_id` around their live state guard. `_state_options_reference_blob`1255 calls `contracts/blobs.py::names_same_blob`929; that helper explicitly assumes its second argument is a **canonical owned metadata id** and only lowercases. An alternate raw selector violates its precondition. Keep this helper's narrower stored-marker contract; normalize the incoming selector instead of broadening the helper to parse corrupt stored markers.
- `get_blob_content`2012 and `read_attachment`2182 retain raw id for result construction after the shared reader; metadata/byte lookup can be successful through UUID custody while the echoed id still partitions coverage.
- `sessions/proposal_blob_refs.py::proposal_blob_reference_ids` and `validate_proposal_blob_references`165 compare/extract textual ids; `pending_proposal_reference_id`204 uses raw membership for retention. D1's new narrow resource-race refusal does not itself establish canonical identity. Preserve malformed owned authority as an integrity/parser failure, not a normalization opportunity.
- `pipeline_planner.py::planner_discovery_information_keys`440-454 formats raw id into metadata/inspection keys and carries string blob id in content/attachment tuples. Equivalent successful queries can be considered separate observations.
- Park extension is a separate small sub-slice: `composer/graph_recovery.py::GraphRecoveryIntent.__post_init__`54 validates only bounded nonblank `park_id`, while `sessions/service.py:6870` compares its raw spelling to durable canonical id. **Do not weaken** `sessions/graph_recovery.py::recovery_identity`33: it protects persisted Tier-1 values and deliberately demands canonical text. Normalize admitted external UUID selectors upstream; keep stored-authority faults intact.

### Smallest grouping / boundaries

Use one owned parsed UUID/canonical-text result at external blob argument admission and carry it through lookup, duplicate detection, guarded read/write, response echo, planner key and proposal reference comparison. Preserve the existing set of accepted UUID forms; normalization is the compatibility-preserving choice, whereas newly rejecting forms currently admitted would require an explicit breaking-contract discussion. Do not silently trim whitespace or accept extra non-UUID forms. Keep canonical stored record representations and sentinel/widened inline-marker grammar unchanged.

Separate raw audit invocation authority from semantic resource identity: the user's/model's actual raw arguments remain auditable and hashed as supplied; canonicalizing a resource selector must not make distinct proposal invocation hashes interchangeable or rewrite historical rows. Prefer canonical identities returned by the existing extractor/owned boundary for lookup/retention to mutating stored proposal JSON. Reinspect current D1 code before choosing that exact seam. Do not change blob-only base staleness D13, recovery supersession D15, deletion policy D5, or D19 encoding/MIME behavior.

### Behavior tests / controls

Start from real session blob rows and actual execute_tool calls, parameterized canonical, uppercase, mixed-case, braced, hyphenless, urn. Assert one record/content identity and canonical response id on metadata/content/attachment/inspection/binding; malformed ids remain typed failures and foreign-session ids remain unavailable without content disclosure. For plural binding, equivalent forms of the same UUID must trigger existing duplicate-document/identity refusal before publication, not be treated as two files. Run both immediate and D1 preview/accept paths, with exact one invocation and no write on refusal.

Critical negative control: for each accepted selector spelling, update/delete of a currently bound blob must refuse and preserve bytes/hash/metadata/receipt/ledger; the same spelling on a real unbound blob succeeds. Cover source blob_ref, nested inline marker, blob_rows blob_id and sentinel/path vocabularies already recognized by the guard. Spy/fail actual mutation when the guard should refuse. Include pending-proposal retention and foreign session controls; no helper-only UUID test establishes this.

Test targets: `test_tools_blobs.py` existing UUID/missing/cross-session/reader fixtures; `test_blob_inline_tools.py` retained-vocabulary/integrity tests; `test_set_source_from_blobs.py`; `test_promote_set_source_from_blob.py`; `sessions/test_proposal_blob_refs.py` and real proposal accept suites; planner manifest tests around `test_pipeline_planner.py:1755-1787`, plus real scripted loop showing equivalent spelling does not create a second unresolved key. Park follow-on uses `test_graph_recovery.py`, `test_planner_graph_recovery.py` and actual service recovery route tests; retain bad durable identity faults.

Prerequisites: integrate D1/D2 first; source/review gate for raw invocation hash vs semantic selector identity; session persistence/retention tests on PostgreSQL. No migration or new lifecycle. Scope means no historical rewriting of arbitrary malformed authority.

## 2. Honest inventory windows, stable ordering and planner selector keys

### Current source evidence

- `blobs.py::_sync_list_blobs`534 and `_sync_list_ready_blob_inline_descriptors`559 query `.order_by(created_at.desc()).limit(50)`. First lists all statuses, second ready only; neither exposes continuation/completeness. Ordinary root is a list; inline root is `{blobs:[...]}`.
- `BlobServiceImpl.list_blobs(session_id: UUID, limit: int | None=50, offset: int=0)`2855 already supports offset, but its order also lacks id tie-breaker. REST routes361-372 exposes `limit`1..500/offset; `frontend/src/api/client.ts::listBlobs`1573 and `stores/blobStore.ts::loadBlobs`94 call default first page only.
- `pipeline_planner.py::planner_discovery_information_keys`437/439 unconditionally supplies `blob.index.session`/`blob.index.composer`. `_resolved_discovery_information_keys`514 recognizes completeness **only for list_models** and otherwise returns those keys without reading inventory size. This re-establishes the audit's false “all gaps closed” path from source.
- Inspection key443-445 uses only blob_id, not `encoding`. Assistance key420-422 uses `issue or 'general'`; handler semantics distinguish omitted selector from empty input. Keep selector distinctions after argument admission and canonical resource identity from group1.

### Smallest ready slice

Pin and use deterministic `created_at DESC, id DESC` in both tool listings and the existing service list, retaining each query's exact session/status scope. Disclose “up to the50 most recent...”/status rules in declaration and user text; correct the repair instruction that pretends `list_blobs` can conclusively rediscover an omitted file. With current fixed-window wire shape, **do not close** an inventory information gap when returned length reaches50 (it might be truncated); under50 successful rows can close it. This conservative boundary makes no promise that a50-row inventory is complete. Invalid/failed observations must not close a successful-coverage key.

Include each admitted result-changing selector in planner observation identity (encoding; exact issue_code/null/empty distinction; canonical resource ids from group1). This is observation bookkeeping, not D7 reread refusal/caps, D24 contract deduplication or D37 planner code-disclosure policy. Preserve legal repeated reads and provider budgets.

Tests: actual SQL rows0/1/49/50/51, equal created_at with opposed insertion order, foreign-session rows, pending/error versus ready-only rows. Assert exact expected membership/order from `(created_at,id)`, not merely that two calls repeat the same arbitrary order. Real scripted planner loop must withhold gaps-closed after50/51, permit it under the successful small complete window, and preserve failed-observation behavior. Parameterize list_blobs and list_composer_blobs rather than extrapolate one. Keys: same id/same encoding repeats identity; distinct supported encodings yield distinct observations; omitted and empty assistance selector follow actual admitted outcome. Keep list_models completeness control untouched.

Targets: `test_tools_blobs.py`, `test_blob_response_contracts.py` (existing patched-query tests do not prove SQL order), `test_pipeline_planner.py` manifest1755+ and scripted-provider helper `_plan`792; `tests/unit/web/blobs/test_service.py` query controls. Frontend description tests cover all three tool text consumers if text changes. PostgreSQL is needed for database-order assertions, not assumed from SQLite rowid behavior. The audit's lane probes list_blobs/test_probe_n1.py supply a dated scenario, not newly passing tests.

### Full pagination is the next design dependency, not solved here

Tracker03.06-03.08/03.10-03.11/03.17 remain open: new page args and completeness wire shape, stable session-scoped continuation, malformed/stale/foreign cursor refusal, strict response/redaction admission, recorded-response compatibility, browser traversal and concurrent create/delete/readiness contract must land together. The audit explicitly requires declaring live traversal versus snapshot consistency; bare list cannot gain additive root fields. No existing D1-D38 product ruling blocks pagination, but do not invent compatibility/snapshot guarantees in implementation. Produce a bounded reviewed design using existing service/query authority, then execute it with real mutation interleavings. Raising/removing50 or teaching the bound alone is not full BOS-01 remediation. Park discovery beyond100 remains separately open03.16.

## 3. Cache-hit audit equals the delivered admitted result

### Current source evidence and smallest change

`tool_batch.py:1612-1632` reconstructs/normalizes/re-admits cached discovery and serializes a full `cached_json` to the provider; `finish_success`1641 instead receives hand-built `{success,data,cache_hit}`. It omits actual current validation/version/affected_nodes and any other admitted optional envelope facts. This is different from the ordinary dispatcher (`audit.py:1259` uses `_result_to_audit_payload(success_result)`). `ComposerToolInvocation.cache_hit` already records cache provenance independently.

Use the exact same owned admitted result/envelope that produced provider content as audit result payload. Keep cache provenance in invocation.cache_hit; if preserving legacy payload.cache_hit is needed, treat it as declared audit-only metadata and compare provider envelope after removing that explicit metadata. Do not rerun the handler, reuse stale validation from the first miss, serialize an unadmitted producer, or add normalization/mapping fallbacks that mask canonicalization faults. No cache/budget/reread/progress policy change. Preserve the admitted response's Pydantic normalization and prompt-guard content-part ownership.

Test targets: `test_service.py::test_cache_hit_rebuilds_result_envelope_from_current_state`4670 already performs discovery -> mutation -> hit; extend to compare actual delivered hit envelope against audit result and durable private response evidence, with version2/current validation and unchanged discovery data. `test_compose_loop_audit_wiring.py::test_cache_hit_audit_preserves_pydantic_payload`1183 currently **intentionally documents/asserts the slim shape** at1263-1267; update those assertions only as part of this behavior change, preserving one real handler call, two invocation rows, miss/hit flags, Pydantic payload and no sentinel. Cover both selected response-contract and legacy cacheable results where both remain reachable; use the live declaration registry to choose representatives rather than assume a tool still lacks a contract. Preserve malformed-cache/readmission fault control (no synthetic SUCCESS/provider egress), private evidence hash, redaction, one invocation per attempted call and no repeated persistence.

No product decision, migration or lifecycle addition. This changes newly recorded payload shape only; keep existing audit readers able to read historical slim results. Gate intentional schema/redaction/snapshot bytes through real producers, not hand-edited expected files. Source file overlaps D1: wait until D1 serial implementation/review finishes and rebase tests on its final cached-outcome code.

## Explicitly held out

Do not fold open product rulings into these fixes: planner diff removal/redefinition D8; discovery limit/reread refusal D7; failure contamination of mutation prior_validation D30; blob content page validation/size D27; queue review reconciliation/recovery D29; diagnostics/code catalogue D25 or D37; restore supersession D15. SG-07/08.01 queue field preservation is a strong separate fallback if group2 pagination design grows: intrinsic queue contract already refuses bulk queue+scope and incremental construction drops scope first (`transforms.py:614-635`). It needs its own bounded source map and actual incremental/bulk/MCP controls; do not conflate it with unresolved queue-review policy.

All selected items remain unchecked. Before implementation: run their audit scenario's current production path and its positive/negative controls in isolated tree, record final HEAD, then failing regression/mutation evidence, passing affected tests, relevant whole-tree gates and independent review. This triage establishes no passing behavior and changes no tracker state.
