# Composer follow-up investigations

Latest disposition: the prefix index experiment is deferred from the final
build after mixed matched results and 5.68% higher aggregate candidate CPU.
The exact original source was restored; isolated implementation, tests and
measurements remain preserved. Its scaling improvement did not establish a
whole-turn benefit or justify unmeasured allocation cost. This does not prove
a general performance regression. See the profiling report for the full
keep-or-defer rationale.

Matched recovery completed four cases with four failures and unchanged source
snapshots. Both same-host cases failed the 15-second resume-completed seam
while building the validation manifest after approximately ten seconds of
imports. Main's web-hosted case also failed during resume-manifest work;
the feature snapshot's web-hosted case failed leader readiness after about
11.7 seconds of imports. These are baseline-observed failures, not recovery
acceptance or proof of one exclusive environment cause. Raw results:
`work/.cache/matched-recovery-20261108T141021`. The feature snapshot still
contained the subsequently deferred prefix experiment; final gates target
the actual restored source.

The user clarified that ADI is not deployed and authorized a source-based fix.
A new endpoint-only operator default is implemented through
`ELSPETH_WEB__AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` (Web) and
`azure_document_intelligence_endpoint` (CLI settings). It is materialized into
owned effective configuration before validation and execution; explicit node
values take precedence, including explicit invalid values which are rejected.
This is a new documented setting, not an inferred deployed binding. Credential
selection is unchanged. The latest focused run passed all 37 ADI cases and two
boot-probe cases (`work/post-gate-focused-3`, unchanged source, exit 0).

These investigations concern the local cache implementation descended from
`1a97c2c04e69c5dd2e5971f2b7e5fe718567ad84`, not verification of a deployed image.
Cache completion and whole-turn profiling take priority. Resumable continuation
is a future resilience feature, not a replacement for reducing turn cost.

| Report | Current disposition | Evidence and next step |
| --- | --- | --- |
| Hidden 1,024-character review line cap and misleading content error | Fixed in current source; direct reproduction passed | The exact reported 1,148-character paragraph passes unchanged through five validation boundaries. Single lines of 1,024, 1,025 and 8,192 characters and a multiline draft containing a 4,096-character line pass. Template/control/credential negative controls still reject. Explicit 8,193-character input gives the inline limit and staged-draft recovery. Handler persistence/freshness tests were inspected, not rerun by this direct probe. |
| Advisor gateway rejection described as unavailable model | Fixed in current source | Closed `request_rejected` category, nonretryable handling and publication withholding exist. Exact historical gateway bound remains unknown; no context-window/quota diagnosis is established. |
| Whole-turn deadline during primary/Advisor work | Still possible; optimisation investigation | Primary requests can consume the remaining whole-turn budget. Advisor is bounded by its configured timeout and the remaining turn. There is no ordinary primary-call reservation for final review. Measure local work, provider calls, payload growth and repair iterations; controlled-provider profiling does not establish historical upstream latency. |
| Advisor timeout wording | Integrated; focused repaired controls pass | Value-free timeout wording preserves audit taxonomy, withholding, retries and the shared deadline. First-wave backend qualification had four fixture failures; six repaired focused cases passed, including fresh-policy controls. No final broad backend pass is claimed. |
| ADI operator endpoint binding | Implemented; focused qualification passed | New documented endpoint-only Web/CLI defaults, explicit precedence, invalid-origin refusal, YAML/runtime parity and recovery binding are covered by 37 passing ADI cases. No deployed instance exists to verify. |
| Gate fork-to-two-sinks graph edges | Integrated; focused component checks pass | Missing fork-to-sink edges are projected while explicit edges, route labels and failure visibility remain. Runtime routing is unchanged. Focused UI and actual Chromium SVG checks passed, including both sink paths with failure routes hidden. |
| ADI mode/schema display | Partial display fix integrated; focused checks pass | Configured model_id/source_mode appear in the missing-catalog inspector fallback alongside schema. This does not claim broader graph-card summaries are implemented. Chromium verified both the unloaded-catalog fallback and configured fields after a schema 503 through the real catalog-open path. |
| Node name followed by “config” | Integrated; focused checks pass | Selected-inspector heading drops the suffix. Canvas labels already used just the name. Exact selected headings passed in Chromium. |
| Document source appears as JSON | Display improvement integrated; original screen unconfirmed | Document filename/type/size now form a readable summary, with full metadata in an expandable formatted-code view. Structured rendering already existed elsewhere. Malformed collections retain the generic renderer; no configuration changes. Chromium verified summary visibility, disclosure behavior and exact complete metadata. |
| Earlier “Composer failures after a staging upgrade” attachment | Full text received; source audit complete, historical causes not all resolved | Stale gateway namespace was operationally repaired in the report; current deployment replacement is unverified. Guard refusals are not proven false positives. Historical five-second preflight slow stage remains unknown. See `work/staging-upgrade-report-disposition.md`; no guard bypass or deployment change was made here. |
| Earlier complex “Bug Rep” attachment | Awaiting accessible full text | Do not substitute later, separately pasted reports for this unseen report. |

No deployment, public issue publication or merge is implied by this inventory.
The original source-session transfer archive is not present in this environment;
its untracked contents remain unverified.

## Additional performance findings from main

The user supplied a source/controlled-benchmark audit at `3b28d537`. Its separate
component timings are not additive and do not establish a whole-turn speedup.
Current-source comparison against the cache branch gives these priorities:

The expanded follow-up scope explicitly includes all supplied main-profile
findings. The following inventory distinguishes focused qualification,
integrated changes and remaining work. It does not claim final release acceptance.

| Item | Status / acceptance required |
| --- | --- |
| Exact dispatch-evidence retrieval | Integrated after final qualification of 143 passes and 41 backend-specific skips across SQLite/psycopg/psycopg2 and Astra review; JSONB filters identity while original-JSON ordinality preserves returned numeric decoding and matching-evidence checks. Matched returned-byte/timing measurement is pending. |
| Repeated committed-proposal history verification | Integrated same-transaction batching; each proposal retains independent authority verification. Factorized proposal/history measurement and broad integrated gates remain. |
| Catalog summary/schema rebuilding | Covered by current cache implementation; focused catalog regression controls passed. Matched bootstrap timing still pending. |
| Duplicate readiness/bootstrap snapshots | Integrated one fresh assessment snapshot and warm frontend catalog custody matching; 92 isolated backend cases passed. Cold bootstrap consolidation remains; full frontend run had one timeout, followed by an unchanged serial module pass (details below). |
| Per-card shared review preparation | Integrated shared cohort preparation; 73 isolated focused cases passed, including rollback, state/head and blob changes. Integrated transaction/authority gates remain. |
| Repeated anchor/current/surfacing record hydration | Integrated bounded transaction-local immutable-record reuse. Live head reads, locks and rollback behavior remain; focused cohort controls passed. |
| Same-node/kind unrelated-term review amplification | Integrated earlier exact Python-trimmed term filtering before unrelated graph hydration. Pending event rows are still scanned; this is not elimination of every quadratic path. |
| Overlapping full candidate validations | Content-keyed validation plus policy-only component probes integrated. The 31 new probe controls prove old structural walks occur and optimized probes omit them, while final graph validation, current policy/default changes and exact diagnostics remain. Broader candidate recount remains a separate measurement. |
| Candidate rebuilt during commit | Investigation remains; a prepared candidate must not replace commit-time authority, custody or content checks. |
| Duplicate review-authority and graph-review restoration | Integrated removal of duplicate restoration with typed ownership and corrupt-record refusal retained; included in focused backend qualification. |
| No-op reconciliation replaces unchanged state | Integrated identity reuse only for unchanged owned state after checks; focused controls preserve stale-review rejection and defensive ownership. |
| Repeated proposal audit-payload digest | Integrated memo of the locally recomputed, verified public digest on immutable authority. Fresh reads still verify; replacing the row resets the memo. Focused backend controls passed. |
| Repeated provider copying, canonicalization, sizing and hashes | Integrated canonical manifest-string reuse and one Advisor section rendering per attempt. Bounds, SDK isolation and physical receipts remain. Private-capture ownership/copying and a fully prepared request remain open. |
| Adjacent canonical set_pipeline schema construction | Integrated one same-attempt construction; no cross-attempt stale schema reuse. Focused request tests passed. |
| Rebuilt runtime validation manifests | Integrated bounded per-build module AST class lookup. Source parity/invalidation and manifest drift controls passed in focused execution selections; complete manifest reuse across admissions remains open. |
| Repeated application source/dependency fingerprint | Cross-call memo deferred: no complete independent deployment/reload invalidation identity exists in current callers. Fresh admission/recovery computation remains; see disposition below. |
| Duplicate row input/output hashes | Integrated reuse of the repository's authoritative input digest for transform/gate attempts with focused retry controls. Output and aggregation hashing remain independent because current output objects do not prove immutable ownership. |
| Repeated recovery-envelope parsing | Integrated one parse in the combined synchronous recovery worker. Actual file-backed user/server stores and snapshot factory passed worker/rotation/revocation controls. Standalone admission and file checks remain; process/cancellation gates are pending. |
| Private-audit deduplication and review-receipt JSON scans | New PostgreSQL profiling lead: growing cursor time inside the session write lock. A proposed expression index failed real storage-domain controls (6 failed, 14 passed, 7 deselected): escaped-NUL JSON accepted without the index is rejected by index creation/insertion. A later guarded index also narrowed accepted nesting depth (bare 16,375 versus indexed 16,335). Both index designs are rejected; production schema remains unchanged and safe query/revision-bound alternatives remain open. |

## Measurement checkpoint — 8 November 2026

The SQLite controlled matrix at local HEAD
`1a97c2c04e69c5dd2e5971f2b7e5fe718567ad84` plus recorded dirty source completed
16 CPU runs (320 healthy turns) and two small-graph memory runs (40 healthy
turns). Every included turn completed its expected tool work and matching final
ADMIT review. Large-graph discovery allocation tracing failed at the separate
five-second runtime-preflight budget before its first healthy turn; the driver
therefore did not run the last large-graph mutation memory condition. This is
not a measured whole-turn timeout and does not qualify the missing conditions.

Small-graph discovery median process CPU rose from 1.657 seconds at turn 1 to
4.542 at turn 20 across three cache-enabled repetitions. Corresponding SQL
statement medians were 474 and 491. The separately traced small discovery run
retained approximately 4.09 MiB of Python heap above its post-seed baseline at
turn 20; that measure does not cover native allocations or establish absence of
a leak. Cache-disabled controls have only one repetition per workload; no
universal speedup conclusion is supported.

A live-provider discovery pilot completed in 12.133 seconds with 2.190 seconds
of process CPU and 9.768 seconds inside the SDK across three calls. Provider
prompt-cache hits were present. A preceding pilot stopped at its explicit call
guard after an Advisor semantic repair was needed. Six returned calls across
both attempts reported total cost $0.0152071499. Neither pilot is a matched
comparison with the separate main-branch tutorial run.

The first PostgreSQL attempt stopped during startup because the profiling
fixture inherited development placeholder signing keys. It measured no turns.
The profiler now supplies fresh in-memory keys for its isolated PostgreSQL
fixture, preserving the real external-state checks. The subsequent matrix
completed six runs (120 qualified turns), then stopped on a runtime-preflight
plugin error in turn 11 of its seventh run. That entire partial run is excluded
from headline aggregates. A separate 20-turn diagnostic did not reproduce the
error; its original underlying exception remains unknown. For small discovery,
three-run median CPU rose from 2.125 to 5.993 seconds and median wall time from
3.092 to 15.276 seconds between turns 1 and 20. SQL statement medians rose from
503 to 585. These are controlled-provider results, not a matched main comparison.

These results precede integration of the follow-up drafts. They do not verify
the drafts, the final broad test suite, or a deployed build. Raw local receipts
and source fingerprints are under `work/profile-results`; preserve them when
moving this worktree.

## UI qualification after profiling

The first integrated UI run produced 178 passes and two failures in the new
document disclosure's identifier-hiding checks. Replacing its markup with
genuine `pre`/`code` elements fixed those failures without changing the shared
DOM check. The subsequent scope passed 206 distinct tests across OptionRows,
GraphView, PipelineSpecView, graph topology and route visibility. Frontend
application/OIDC typechecking and scoped ESLint passed. Seven expected assertion
failures against old production code demonstrate that the new tests detect the
reported display/fork differences. Receipt:
`work/.cache/ui-wave1-repair-receipt.md`.

The full frontend unit run finished with 4,448 passes and one timeout (263 passed
files, one failed; 549.62 seconds). The quota-admission Stop case took 5,346ms
against its unchanged five-second limit. Its unchanged whole module then passed
all 11 cases serially in 6.76 seconds. This is not a claim that the original full
run passed. Application/OIDC typechecking passed.

Actual Chromium qualification passed the new display case and the existing
keyboard-inspector case across scoped runs; two deliberate keyboard-suite skips
remain. Fresh production builds, workspace-browser typechecking and scoped lint
passed. SVG fork edges, document summary/full metadata and ADI unloaded-catalog
and failed-schema fallback were verified. Automatic readiness validation was
answered locally with an explicit 503; no backend/provider execution occurred.
A separate service-notice overlay can block the catalog's pointer close button;
Escape worked in that run. The subsequently integrated 44px notice offset passed 61 UI cases, type/lint/build checks and Chromium pointer/keyboard/focus checks; see `work/.cache/catalog-offset-qualification.md`. Earlier fixture attempts remain
failed attempts. Receipt and screenshots: `work/.cache/browser-display-receipt.md`.

Request-preparation, owned-state identity and timeout wording are integrated.
Their first combined backend run completed with 797 passes and four failures
(349.44 seconds, exit 1). Six focused cases passed after fixture repairs described
below; this does not make the final broad backend gate green.

## PostgreSQL compatibility qualification

The first exact proposal-evidence projection also failed real PostgreSQL
compatibility controls: six failed and four passed. Existing JSON records can
contain unrelated escaped NUL or numeric values outside JSONB's domain, which
the original reader accepts but a JSONB cast rejects. The subsequently qualified narrow savepoint
fallback is integrated, as recorded below. Unknown
database errors must propagate, original transaction writes must survive, and
duplicate or malformed matching evidence must still fail integrity checks.

The separate private-audit expression index is rejected and unintegrated. Its
later guarded implementation still narrowed accepted JSON depth (16,375 without
the index versus 16,335 with it). No production schema change is justified.
The original CPU instrument failure was repaired and its controls requalified;
completed attribution runs are recorded below, separately from normal latency.

## Integrated qualification update

The four first-wave backend failures were repaired in two test files only:
two new Advisor fixtures had incorrectly selected the same model for both roles;
two expected an intentionally removed immediate duplicate policy projection.
Six focused cases passed (exit 0), including fresh-policy refusal controls.
Astra approved the repairs and integration of the subsequent review-batch,
readiness, execution and proposal-reader patches. Execution qualification covers
340 distinct cases across scoped runs, not one final broad rerun.

The proposal reader's final qualification passed 143 cases with 41
backend-specific skips across SQLite and both PostgreSQL drivers. JSONB filters
identity, but the original JSON element at its ordinal supplies the result,
preserving legacy float/large-number decoding. The narrow savepoint fallback
covers escaped NUL, overflow and measured lone-surrogate JSON cases (22P02).
Astra approved the reader. This approval does not extend to the rejected index.

Catalog reconciliation passed 11 focused controls: unsupported discovery sets
remain unsupported and are omitted from evidence rather than coerced; mutable
returns are isolated; unrevisioned/subclass producers do not retain stale cached
results. This is distinct from registered plugin JSON-schema emission, which
correctly rejects non-JSON defaults before serialization and preserves valid
large integers.

CPU attribution controls are now qualified. The initial failure was an
instrument error: Yappi's convenience ctx_id=0 selector returned all threads.
Explicit context filtering and thread-identity negative controls now pass;
allocation retained/freed and preexisting-tracing controls also pass. CPU and
allocation attribution each completed 20 healthy turns on identical recorded
pre-prefix source. CPU tracing found reference/partition generator calls growing
from 1,332 to 2,135,982 between turns 1 and 20. Retained allocation totals were
35.682 MiB after turn 1 and 45.636 MiB after turn 20; turn 20 released 27.396 MiB
net. Instrumented CPU is not normal product latency, and these heap observations
prove neither a leak nor its absence. The largest retained pathlib allocation's
caller remains unestablished.

## Paired prefix measurement and unresolved acceptance

The prefix index passed 165 focused tests and Astra review. Three alternating
20-turn controlled SQLite small-discovery pairs then completed: 120 healthy turns
across six qualified fresh-process runs, with fixture-map and before/after source checks. Only the
prefix algorithm differed; this is not a main-branch or live-provider comparison.
Whole-run CPU reductions were +14.96%, -13.75% and -21.29% across the pairs, so
there is no consistent whole-run speedup. Turn-20 median CPU was 4.238s before
and 3.899s after, while median SQL statements remained 494. These scoped terminal
results do not establish a universal improvement or absence of memory growth.
Evidence: `work/.cache/prefix-paired-final/{summary.json,receipts.ndjson,complete}`.

The residual xfail corpus is not cleared or adjudicated against baseline:
114 unexpected/unreviewed entries, 74 unresolved executions, three stale writer
entries and seven stale read entries remain; these categories overlap. Do not
label them inherited. Finite guard repair passed 10 cases with one existing xfail;
the ordinal-helper seal control passed separately. The 17 cache benchmarks passed
in 12.19 seconds; whole-tree Ruff checks and formatting checks over 3,991 files
passed. Matched process recovery subsequently failed as described at the top
of this inventory; two global proofs and final canonical full gates remain
unrun. Focused repairs and measurements do not replace these release gates.

## Fingerprint and complete-manifest reuse disposition

Current admission and recovery compute application fingerprints freshly from
source paths/bytes, installed distribution metadata and exact available plugin
IDs. The resulting fingerprint is itself the deployment generation, not an
independent invalidation identity. Snapshot/PID/mtime/size keys cannot cover
same-mtime source changes, source additions/deletions or dependency changes.
No new cross-call memo is justified without an enforced immutable deployment
identity or a proven same-input duplicate inside one authority evaluation.

Runtime-VAL already has bounded within-build reuse, including the module source
index, and a restricted four-entry frozen-registry manifest LRU for run headers.
That registry membership identity does not cover live methods, closures,
annotations, globals or source bytes. Approval evidence still uses the fresh
builder; silently sharing the run-header LRU would widen its authority scope.
Complete cross-call manifest reuse remains deferred pending an immutable-code
lifetime/invalidation contract and the existing drift controls. Neither finding
is fully resolved. Source-only disposition:
`../composer-prompt-prefix/work/fingerprint-manifest-disposition.md` relative to
the local worktree directory; no new timings were claimed by that review.

## Latest integrated qualification

All 12 originally failing recovery cases passed serially at unchanged deadlines
(`work/post-gate-recovery-1`: 12 passed, 2 warnings, 1,197.59 seconds; exit0,
source unchanged). This supersedes their unresolved serial status, not the
original failed broad receipt. All 69 probe/ADI/previous-worker-crash cases and
six repaired PostgreSQL module cases also passed. Two other original PostgreSQL
failures passed unchanged in `work/post-gate-postgres-1`; that run retained the
acknowledgement failure subsequently repaired. A final integrated default and
serial PostgreSQL gate remains required. Broader Sessions inventory drift and
operator signature clearance are not resolved by these focused passes.
