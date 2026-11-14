# Task 1 blob identity review

**Spec verdict: PASS for the bounded Task 1 implementation.** The patch implements the accepted external-selector contract and the four design-review clarifications. Task 2 is held out. The plan's combined merge verification remains outstanding and is not included in this verdict.

**Code-quality verdict: PASS for integration, with no blocking code finding.** The three added R6 diagnostics are justified parse-rejection flows, as adjudicated below. The five architecture locator edits are justified, bounded repairs. This is not a full-suite pass, signed-metadata clearance, merge approval, or closure of R2/XC-13.

## Reviewed candidate and method

Reviewed Task 1 and the global constraints in `resource-identity-plan.md`, `resource-identity-design-review.md`, repository `AGENTS.md` and relevant `CONTRIBUTING.md` requirements, the implementation patch and report, changed behavior and its callers, the unchanged receipt writer/reader and marker comparator, and the saved execution evidence.

Implementation tree: `/workspace/elspeth/.claude/worktrees/composer-remediation-resource-identity`.

Measured HEAD: `1bc91710fbbdd205376f71dadf8aa3ca557229d8`.

Reviewed patch: implementation-tree `.claude/lanes/composer-remediation-wave1/blob-identity-review.diff`, SHA-256 `0b31812f5559ac9326ab7b13cadf8e020924d4309f05c523816ca1d53ccd90a3`.

I independently recomputed that patch hash and all 16 hashes in `blob-identity/source-freeze.json`; all matched. The recorded set comprises five production files and eleven test files, with four new files overall. I also compared the five final production hashes against the before/after snapshots for `green2`, `affected`, `postgres`, `negative-controls`, `mypy`, `lints-after`, `gates`, `inspections`, `authority-repair-final`, `final-static` and `final-format`: every production hash matched in every capture. No production changes occurred between those captured cohorts and this review's hash check.

This review used static source inspection, saved log/exit inspection, scoped AST comparisons and recomputation over existing evidence. It did not rerun pytest, lints, broad source scans, providers, installs or mutation cohorts. No implementation or test edit, commit, signing, suppression or Page was made. Only this lane review was written.

## Findings and dispositions

No new P0/P1/P2 implementation defect found in the reviewed scope. No implementation change is requested by this review.

The remaining verification limitation is material to merge readiness: the full default suite, separately scheduled long proofs and full serial PostgreSQL selection remain parent-owned and unclaimed. The 31 PostgreSQL cases below prove the selected contention paths, not every PostgreSQL behavior. The original 1089-test gate remains an exit-1 result; its diagnosis and subsequent targeted repair do not retroactively turn it into an all-green full gate.

## Spec assessment

### Selector identity, scope and bindings

`web/resource_identity.py:6` implements exactly the requested string-only `str(UUID(value))` projection with `ValueError -> None`, without stripping whitespace or imposing a UUID version restriction. The handler paths obtain the canonical string after their existing argument-model admission. The older `_blob_id_uuid_validation_error` retains its explicit non-string refusal and existing messages; successful handler paths retain the parsed selector instead of validating and discarding it.

Metadata, inspection, singular and plural source resolution, inline wiring, mutation admission/execution, and both content readers use the canonical selector. Session predicates, existing typed service calls, custody locks, operation authorities and recovery observation remain in place. `_sync_get_blob` itself is unchanged. The metadata success payload remains the six-key allowlist; its missing/foreign failure remains the fixed no-ID-echo message. Metadata still legitimately reports a pending record's status, while readiness-dependent operations reject it.

`tools/sources.py:1391` canonicalizes every plural selector before lookup and rejects duplicate UUID identity before constructing a new binding. Bindings and successful identity-bearing results derive from authoritative metadata where available. Content and attachment echo `blob["id"]`; delete echoes the admitted canonical selector. This is the intentional successful response-value correction stated by the plan, with unchanged envelopes, paging, expected-hash and byte-integrity logic.

The real SQLite tool matrix uses the alphabetic `abcdefab-cdef-4abc-8def-abcdefabcdef` fixture for canonical, uppercase, mixed, braced, hyphenless and URN forms. It exercises real tool execution, canonical binding/echo values, invalid and whitespace selectors, foreign/missing/non-ready records, and both bound refusal and unbound effects. The strengthened nested case is a complete inline marker, and the sentinel case uses a path distinct from the physical storage path. Refusals assert unchanged state, bytes and rows. Plural duplicate tests preserve the prior binding object.

### Strict markers and conservative projection

`contracts/blobs.py::names_same_blob` remains unchanged. The patch supplies it a canonical target; it does not widen embedded-marker syntax. The production inline discovery test demonstrates that braced/hyphenless embedded markers remain unexecutable even though proposal custody projects their resource identity canonically.

`sessions/proposal_blob_refs.py:104` projects only the pre-existing closed tool/position vocabulary. Nonempty nonparseable IDs remain references, malformed shapes still raise, and strict `blob:` sentinel validation still runs before projection. Uppercase/mixed/braced/hyphenless/URN sentinel negatives are retained. The original argument dictionary is neither rewritten nor replaced. Owned-state authority shape checks, owned persisted-status corruption errors and exact accepting-proposal exclusion remain unchanged.

### D1 and planner facts

Admission normalizes before the live-state retention guard and continues to observe rather than execute an effect. The provider-driven D1 tests cover successful staging and known ineligibility across both dialects, asserting original stored/provider spelling and private argument hashes. A braced ordinary deletion proposal exercises final publication-time resource loss and subsequent repair through the existing typed `proposal_resource_unavailable` path. That race test remains the existing default-dialect harness; the two-dialect matrix is specifically the staging/admission-refusal cohort, not every race variant.

Planner metadata and inspection keys now use the actual `BlobIdArgumentsModel` and `InspectSourceArgumentsModel` before calling the parser. Content and attachment retain their existing models. Malformed UUIDs, whitespace and rejected model shapes yield no supplied/unavailable fact; offsets, expected hashes and different blob IDs remain distinct. The scripted planning-loop test executes alias metadata discovery, observes canonical returned identity, and checks the next provider request closes the initially unresolved canonical fact. Its oracle reflects the real planner behavior, which permits discovery rereads; it does not invent or change a no-gain policy.

### Exact evidence and receipt recovery

The unchanged `proposal_blob_effects.py::proposal_blob_arguments_hash` verifies the projected blob reference but hashes `stable_hash(arguments)` over the received document. The unchanged `mutation_capabilities.py:270` receipt reader recomputes that hash from stored arguments and separately verifies receipt identity, snapshot hash, live state and accepted-event binding. It never repairs a receipt or snapshot by normalizing it.

The extended HTTP acceptance/recovery tests exercise both update and delete, all selector spellings, exact accepting-proposal exclusion with another pending reference, canonical compatibility controls, and a fault after the effect but before proposal settlement. They assert original stored arguments and private/receipt hashes, one effect across retry and duplicate acceptance, and successful settlement. Equivalent-selector-only tampering preserves projected resource identity yet makes both reject/accept fail with `AuditIntegrityError`; the effect counter remains one. Existing snapshot/hash and advanced-state recovery cases remain present. Leaving both receipt modules unchanged is appropriate.

## Captured behavior and negative controls

All paths in this section are under the implementation tree's `.claude/lanes/composer-remediation-wave1/blob-identity/`. I inspected actual capture commands, exits, frozen snapshots and terminal log summaries rather than treating the report's table as execution evidence.

| Capture | Observed result | Bound of evidence |
| --- | --- | --- |
| `affected.json` / `.log` | Exit 0; 950 passed, one Pydantic warning | Eight affected tool/planner/source/proposal/receipt modules; before the later isolated test strengthenings |
| `green2.json` / `.log` | Exit 0; 97 passed, 77 deselected | Scripted canonical-fact closure, D1 staging/refusal and publication-race selection |
| `inspections.json` / `.log` | Exit 0; 171 passed | Existing inspect-source and source-inspection regression coverage |
| `postgres.json` / `.log` | Exit 0; 31 passed | 12 bound-selector cases, six real writer-debt waiters and 13 live-deletion reader/mutator contenders |
| `gates.json` / `.log` | Exit 1; 1083 passed, three skipped, one xfailed, two failed | Relevant architecture/attribute/masquerade/mock/response/schema gates and the final nested/second-pending test strengthenings |
| `authority-repair-final.json` / `.log` | Exit 0; six passed | Both failed gates plus injected mutation/escape and general read-review escape controls, after final formatting |
| `final-static.json`, `final-format.json` | Exit 0; frozen | Ruff check and format check of all final 16 files |

The PostgreSQL tests inspect real PostgreSQL lock waits and competing database state. In particular the live-deletion harness verifies waiting in BLOB_CUSTODY without holding SESSIONS, then observes deletion; the admission waiter observes real competing writer debt without allocating another effect. These are not SQLite extrapolations.

I inspected `negative_controls.executed.py`, per-mutant source provenance and failure output, and `negative-mutants.json`. All eight isolated mutants exited 1 for the intended assertion, with canonical positive controls still passing:

| Mutant | Failed / passed | Observed failure |
| --- | --- | --- |
| PostgreSQL raw guard | 2 / 2 | Bound braced admission incorrectly returns stageable |
| Raw metadata lookup | 1 / 1 | Braced selector is falsely missing |
| Raw execution guards | 2 / 2 | Bound update/delete actually succeed |
| Raw admission guard | 2 / 2 | Bound preview incorrectly returns stageable |
| Raw content echoes | 2 / 2 | Both readers return braces in `blob_id` |
| Raw planner keys | 21 / 4 | Alias key divergence and failure to close the real next-request fact |
| Raw proposal projection | 36 / 6 | Alias custody/deduplication and second-reference retention fail |
| Normalized receipt hash | 4 / 2 | Raw-document hash mismatch, and equivalent-selector tamper no longer raises |

One instrumentation nuance does not invalidate the admission result: the raw-admission substitution matches both admission and update's identical guard expression. Its tests fail at the admission assertion before execution; the separate execution-only mutant independently demonstrates execution protection. The receipt mutant is particularly discriminating: canonical untampered recovery passes for both tools, while canonical-to-uppercase stored-selector tampering fails the expected integrity exception under the bad normalized-hash implementation.

## Authority locator repair

The two original gate failures are `test_inline_csv_comparison_read_admission_is_exact` and `test_live_connection_domain_classification_is_exact`. `authority-base-test.json` and `authority-base.log` record both exact tests failing on the exact archived base, with both import roots inside that archive. The inline CSV locator was already stale by five lines; the Landscape classification expected 935 while base and candidate live code both reported 955.

I independently compared every field and multiplicity of the saved 69-member expected/base/candidate classification rows. Base and candidate are exactly equal, including line locators. Their only expected/live disagreement is the same Landscape `probe_schema_shape` line 935/955. The artifact records nine independent positive field controls and an equal-identity negative control. The initial provenance-invalid ad-hoc probe is explicitly discarded in `invalid-authority-probe.json`; it is not relied upon here.

For all four affected reviewed read functions, I independently parsed the actual base and candidate source, compared complete function ASTs, checked their AST hashes against the artifact, and confirmed current connection lines. Each full AST is unchanged. This includes query bodies and scope guards, not only the connection-call fingerprint.

I independently compared the complete architecture-test ASTs. There are exactly five differences, each a numeric locator constant:

| Binding | Previous | Final |
| --- | ---: | ---: |
| `_sync_get_blob` | 501 | 502 |
| `_sync_list_blobs` | 536 | 538 |
| `_sync_list_ready_blob_inline_descriptors` | 561 | 563 |
| `_header_only_inline_csv_conflict` | 1826 | 1834 |
| `probe_schema_shape` expected classification | 935 | 955 |

No member, authority, fingerprint, ordinal, escape flag, policy or signature changes. The recorded derivation uses the existing production-writer scanner, and the six final tests keep DML and escape refusals active. This is within the parent's explicit five-locator authorization. The 1089-test selection was not repeated; the original exit 1 and the focused exit 0 must remain separate claims. The three skips concern Python 3.13 PEP 696 support; the existing xfail concerns the Sessions writer-authority inventory.

## Lint adjudication and scope

I independently reparsed every structured diagnostic in the saved logs into a multiplicity-preserving `(path, rule, message)` Counter, removing diagnostic offsets and only embedded ` at <path>:<line>` offsets. A known diagnostic and warning negative control worked; no nonwarning line was left unparsed. The result matches the supplied comparator: **5433 before, 5436 after, zero removed, exactly three added R6 occurrences**. Both all-rules captures still exit 1.

| Added R6 site | Disposition |
| --- | --- |
| `web/resource_identity.py:10`, `except ValueError: return None` | Accept. This is the specified malformed external-selector parse result. It catches only UUID parse rejection; it does not catch storage failures, owned corruption or arbitrary exceptions. Callers use `None` to refuse or withhold a fact. |
| `web/composer/pipeline_planner.py:446`, metadata model rejection | Accept. A rejected external argument shape cannot identify an information fact. Returning `()` prevents false coverage; execution's ordinary argument refusal remains separate. |
| `web/composer/pipeline_planner.py:455`, inspection model rejection | Accept for the same reason, using the actual inspection model. No observation or mutation failure is swallowed. |

These are written semantic dispositions permitted by `CONTRIBUTING.md`'s R6 rationale policy; they do not make the raw scan green. The old content/attachment model-rejection arms were already in the baseline. Adding a broad catch, artificial log, altered semantics or suppression merely to silence R6 would be unwarranted.

The companion boundary capture has **205 occurrences / 204 distinct complete messages**, all `R_TB_TESTS_FINGERPRINT_MISMATCH`. My Counter comparison against the base all-rules capture is equal, with no additions/removals. No boundary fingerprint or signed metadata was edited.

The lint captures predate the locator repair, so I do not call them final-candidate input freezes. Production hashes remain identical. `locator-boundary-scope.json` records the controlled inventory of 205 literal production `test_ref` references, zero references to the changed architecture test file, the known YAML-importer positive, a synthetic changed-target positive and an unbound-string negative. I inspected the sole reported nonliteral forwarding site in `contracts/trust_boundary.py:234`: it passes the supplied `test_ref` into `TrustBoundaryMetadata` and constructs no new target. The full architecture AST comparison above confirms only four module-level locator values and one classification-test locator changed. Together these support carrying the boundary diagnostic delta across this narrow repair. The inventory itself was reviewed as existing evidence, not rerun as a fresh broad scan.

No signed allowlist clearance is inferred from keyless shape verification, and no signing or metadata rewrite is requested by this review.

## Integration limits

Proceed with the parent's combined verification and separate Task 2 work. Preserve this patch identity when carrying forward the reviewed result, and rebind verification if production changes. The result supports Task 1's identity-specific fixes and compatibility contract; it does not assert live-provider/browser acceptance, full PostgreSQL coverage, parked-graph behavior, pagination completeness, inspection encoding identity, or umbrella R2/XC-13 closure.
