# Miscellaneous components: related defect search

Date: 2026-09-30. Search began on composite `8fc2f998d` before release
integration. Nine related defects were reproduced, repaired and independently
reviewed in the assembled working tree on `2e707c904`.

This report records the search and focused review. The integration report owns
the final committed candidate, accumulated gates and release-merge verdict.

## Method

The requested `using-systems-thinking` skill directed the search from each
original repair to its neighboring consumers. The question was: which fact
must survive this boundary, and can a consumer change or discard it while
still reporting success?

| Original repair class | Adjacent boundary searched |
| --- | --- |
| Strict snapshot options | Authored config → normalized runtime values → required-control credit |
| Snapshot version and quarantine identity | Persisted evidence → owned row reconstruction |
| Payload dependency retention | Destructive deletion → grade update → retry |
| Response limits and embedded-address SSRF | Shared HTTP refusal → plugin result; logical URL → DNS/Host/SNI |
| Source approval reconciliation | Planner proposal → source binding → preserved approval authority |
| Owned audit metadata | Receipt → result token; sink attribution → member identity; outbox → journal publication |

Searches had known-positive and known-negative controls. Retained findings
required actual public callers and discriminating reproductions, rather than
similar spellings. The mechanisms are representation drift, evidence loss and
incomplete retry admission; no feedback archetype or incident rate was inferred.

## Confirmed defects and repairs

The descriptions below refer to the search candidate before these repairs.

| Defect | Reproduction and repair |
| --- | --- |
| Ordinary snapshot rows erase quarantine facts | A real committed, hash-valid spool with `is_quarantined=False` and non-null quarantine error/destination was admitted; the valid-row factory erased those facts. [Snapshot decoding](../../src/elspeth/engine/orchestrator/source_snapshot.py) now refuses the contradiction before construction. Valid ordinary/quarantined controls remain. |
| Purge retry loses a failed grade update | After a real response-blob deletion and one transient fenced SQL failure, rediscovery found the ref but retry skipped its outstanding downgrade. [Purge](../../src/elspeth/core/retention/purge.py) now reconciles confirmed absence, including already-missing blobs, through the existing fenced writer. Counts remain accurate; failed/dependency-held deletions do not prove absence. |
| BlobFetch leaks an encoding refusal | Unsupported or malformed response coding raised the new shared exception out of the public transform; WebScrape returned a row error. [BlobFetch](../../src/elspeth/plugins/transforms/blob_fetch.py) now catches the concrete owned encoding-limit exception. Valid responses and exactly-once error audit controls pass. |
| Unicode hostname differs from wire identity | A valid `bücher.example` URL passed admission but both HTTP plugins raised `UnicodeEncodeError` when encoding Host. [Shared URL security](../../src/elspeth/core/security/web.py) now derives the same ASCII hostname as HTTPX before DNS/Host/SNI and archived-pin checks. Logical URLs remain audit identities. [Browser records](../../src/elspeth/core/browser/boundary.py) use the shared origin parser, keeping admission/archive/offline replay consistent. |
| No-op safety thresholds receive required-control credit | Actual REQUIRED policy validation credited four `6.0` or `"6"` thresholds, although runtime admission normalized them to integer six and blocked no severity. [Azure Content Safety](../../src/elspeth/plugins/transforms/azure/content_safety.py) now evaluates its runtime-owned threshold model; no-op and malformed thresholds lose credit, effective values retain it. |
| Source echo erases approval or blocking drift | Public `set_source` with an unchanged acknowledged source lost its event/hash; an inconsistent accepted hash could lose its blocking integrity finding. [Source mutation](../../src/elspeth/web/composer/tools/sources.py) now uses existing authoritative reconciliation, preserving only coherent applicable evidence and returning original state on refusal. |
| Coalesce retry trusts a divergent result token | Schema-valid changes to result payload ref, row or step were accepted despite an unchanged receipt. [Token replay](../../src/elspeth/core/landscape/data_flow/tokens.py) now compares all three receipt witnesses. Existing receipts are validated before payload storage, so exact/divergent retries neither rewrite evidence nor mint tokens. |
| Sink attribution accepts bool/float ordinal | Public completion accepted `False` or `0.0` as member zero and finalized malformed attribution. [Member completion](../../src/elspeth/core/landscape/execution/sink_effect_lifecycle.py) now requires an exact nonnegative integer before equality; refusal preserves the durable ledgers. |
| Journal recovery accepts bool/float metadata | Public recovery published and acknowledged ordinal `false`/`0.0` and size `true`/`1.0`. [Journal admission](../../src/elspeth/core/landscape/journal.py) now requires exact integers before identity comparison. Strict and relaxed modes preserve journal/outbox bytes on refusal; valid recovery publishes once. |

## Verification

Each defect had a failing original-source regression and passing admitted
controls. Component checks completed with explicit exits:

| Component | Completed focused result |
| --- | --- |
| Snapshot/purge/grade retry | 122 passed, exit 0 |
| Network and browser archive consistency | 297 passed, exit 0 |
| Safety thresholds | 13 new regressions; 209 component/coverage and 96 policy/autowire checks passed, exit 0 |
| Composer source authority | 160 passed, exit 0 |
| Token replay/finalization | 226 passed, exit 0 |
| Member attribution/reservation | 139 passed, exit 0 |
| Journal/restore | 160 passed, exit 0 |

Counts overlap and are not summed. Independent assembled review completed
**78 targeted checks, exit 0**, with all 29 reviewed Python paths unchanged
before/after. It found no unresolved production issue in these nine repairs.
The reviewer also verified canonical source hashes for the changed plugins.
Plugin versions remain their static `1.0.0` declarations, following
CONTRIBUTING; only source hashes were regenerated.

Additional admission, retry and durable-refusal companions exercise existing
call, source replay, aggregation and restore contracts. Selected in-memory
guard mutations made their tests fail. Their measured contributions against
the original coverage dataset are not final coverage-floor clearance.

## Scope and counterexamples

The controlled sweeps recorded 26 config/DTO files; 12 full audit/retention
files plus eight targeted files; 18 network seams; and named Composer mutation,
dispatch, persistence, validation and authority flows. Scopes overlap. Large
files were reviewed through the listed functions, not asserted as exhaustive
file reviews. This is not a repository-wide absence-of-bugs claim.

Validated source flags, strict checkpoint integers, sealed quarantine identity,
frozen dependency mappings, OIDC ASCII endpoint admission and existing
redirect/pagination limits supplied counterexamples. Fourteen detached-DTO
adversarial controls passed. No second reachable Composer or DTO defect was
established in those bounded sweeps.

Authentication remains disabled before HTTP execution, browser foundations
remain unregistered, and Composer provider/tutorial invariants remain intact.
Those deliberate boundaries were not classified as defects.

## Accumulated acceptance

The original frozen `8fc2f998d` default run had 12 integration failures and
59741 passes; its PostgreSQL stage passed 652 tests, exit 0. The integration
failures have separate reviewed repairs. Those earlier results do not accept
the new sibling patches.

That candidate also failed existing critical coverage floors: Landscape
87.759489% versus 92%, orchestrator 88.454212% versus 90%. The release baseline
already failed both floors. Neither floor nor exclusions were lowered.

Following the maintainer's consolidation correction, reversible patches were
collected in the composite and reviewed together. Final accumulated static,
default-selection coverage and PostgreSQL results must be measured there;
incorporating an individual patch does not trigger a full suite. This report
does not claim release merge, push, deployment or operator signature clearance.
