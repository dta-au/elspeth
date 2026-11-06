# Unit-by-unit audit inventory

Each of the 51 U-units and Q had a distinct reviewer in the original full audit at
`a6990ffb72bd8b49e8298fbb4b152d586ce683a5`. The 6 November update rechecks only
previously open portions at `ae88147c6130051b1e3d497a1341a69bd56924e5`.
Completed-unit assessments are inherited, not claimed freshly re-audited.
"Present" below means scoped production paths
and meaningful assertions were inspected, not that all tests passed or every
acceptance obligation was met. The package's gap/quest IDs are local labels,
not issue-tracker IDs.

Reviewer path is `/root/<coordinator>/<unit in lowercase>`; for example,
`/root/audit_runtime/u35b`. Parent coordinators consolidated reports and the root
reviewer independently checked principal contradictions. Reports were inline
under the original read-only audit; this package is the durable handoff.

Focused refresh reviewers were `refresh_rollover` (G01/O02), `refresh_proof`
(G02/O03/O07), `refresh_tests` (G03–G05), `refresh_docs` (G06/G07/O06),
`refresh_acceptance` (O01/O05), and `refresh_residue` (O04). Parent independently
checked principal contradictions, the arriving commit delta, and GitHub O07/O08.
No completed unit was reopened. New work inside already-open runtime/proof items
was reviewed because those files changed.

| Unit | Coordinator | Scope and disposition |
|---|---|---|
| U00 | audit_foundations | Target/base present; later push ruling supersedes historical no-push wording |
| U01b | audit_foundations | Documentation carry substantial; historical seams/handover cleanup O06 |
| U01c | audit_foundations | Cleanup procedure present; designated inventory reconciliation O06 |
| U02 | audit_foundations | Engine E1–E10 paths/tests present; E11 keyless set-diff belongs to Q |
| U03 | audit_foundations | Checksum proposal→HTTP→UI, golden oracle and ACA consumer checks present |
| U04 | audit_foundations | TC003/007 secret/recovery guidance and contract assertions present |
| U05a | audit_foundations | Neutral waiting progress present; initial-segment notice fixed |
| U05b | audit_foundations | Immutable policy, clock, admission and production construction present |
| U05c | audit_foundations | All seven Advisor donor paths compared; live interpretation callers present |
| U06 | audit_foundations | Fork schema/guidance, handler/runtime settings and tests present |
| U08 | audit_foundations | Bounded live graph views, fencing, credentials and full-content guard tests present |
| U09 | audit_foundations | Publication/self-revert connected; obsolete legacy expectation G05 |
| U09b | audit_foundations | Actual row readers use strict decoders; parity/role-refusal assertions present |
| U10 | audit_contracts | Protected security carry and fixture isolation present; installation remains operator work |
| U11 | audit_contracts | Trusted in-process adapter model coherent; minor stale service comment only |
| U12 | audit_contracts | DOC001 present; continuation-result teaching/discovery omission G07 |
| U13 | audit_contracts | Scalar/named/plural roots, private reuse, projection and atomic ownership present |
| U14 | audit_contracts | Exact validation codes and fenced verified reads; real PG lock/EOF opposition present |
| U15 | audit_contracts | MCP TC018/019/020 admission, baseline and audited fault handling present |
| U16 | audit_contracts | Secret inventory metadata versus current resolution correctly separated |
| U17 | audit_contracts | All eight dossier carries present; historical binding O06 |
| U18 | audit_contracts | F0/F1 SQL/executor classification through handlers and durable history present |
| U19 | audit_contracts | F2/F3 owned observer faults escape transport retries and preserve custody |
| U20 | audit_contracts | F4–F7 Advisor/tool/P4/gateway/cancellation paths and meaningful tests present |
| U22 | audit_contracts | Prepared provider capture, guard, quota, post-await verification and SDK seams present |
| U23 | audit_contracts | Real PG publication races, observed lock waits and weakened-guard controls present |
| U24 | audit_contracts | Exact-limit route and PG ordering/failure/cancellation tests now present |
| U29 | audit_runtime | Frozen mutation ceiling/current trust under locks; sticky revocation/races present |
| U30 | audit_runtime | Common loop, planner retirement and retained cost policy connected |
| U30b | audit_runtime | Production hoist connected; removed-method test callers G03 |
| U31 | audit_runtime | Complete evidence connected; historical bounds/representation disposition O01 |
| U32 | audit_runtime | Full-original admission connected; original limit/cache promises differ O01 |
| U33 | audit_runtime | Strict frames, evidence graph, codecs and real reconstruction present |
| U34 | audit_runtime | Epoch88 journal/schema/CAS present; stale PG assertions G04 |
| U35a | audit_runtime | Claim/start, uncertain-start reconciliation and process/guard controls present |
| U35b | audit_runtime | Atomic successor, readback, lease consumption and live driver join present |
| U36 | audit_runtime | New joined Read/Review/iteration/Stop cold handovers strengthen coverage; combined cross-sequence scenario O02 remains |
| U37 | audit_runtime | Later-Compose quantum/watermark rollover now received with real-clock tests; first/non-Compose cuts remain G01 |
| U38 | audit_runtime | Actual settings/persisted policies/deadlines/grace checks present; G01 counted once |
| U39 | audit_runtime | Live reaper and substantive PG/process/paid-cut recovery present; full matrix O05 |
| U40 | audit_completion | Provider-offered control and actual worker successor completion present |
| U41 | audit_completion | Single-lifecycle HTTP/SSE/replay/Stop/replica paths present; full matrix O05 |
| U42 | audit_completion | Observer/store/custody/notice paths and source assertions present |
| U43 | audit_completion | Auth/capability distinction and strict source-option no-op behavior present |
| U44a | audit_completion | Current atomic TITLE PG settlement/rollback/duplicate/cold reclaim coverage verified; exceptional-case mapping remains O05 |
| U44b | audit_completion | Live P4 audit/transaction/application and restore/fault tests present |
| U45 | audit_completion | O03 now includes verified existing R220 preparation failure on matching code/tests; full profile still outstanding |
| U46 | audit_completion | Proof machinery present; two of 13 full-source guards stale at ae881, down from seven |
| U47 | audit_completion | CLI config/flag precedence, dry-run and offline retention instructions present |
| U49 | audit_completion | Donor path reconciliation substantial; complete current semantic partition O04 |
| U53 | audit_completion | Version surfaces present; epoch/policy inconsistencies G06 |
| Q | audit_completion | c9f CI cancelled; final ae881 CI lookup queued after publication; G02/O03 concrete blockers; full qualification O07 |

## Prior findings closed at a6990ffb — inherited historical evidence

These closures and line pointers come from the original full audit. They remain
credited and were not rerun or comprehensively re-audited for this refresh.

| Old concern | Current evidence to preserve |
|---|---|
| Continuation control not exposed/dispatched | `provider_gateway.py:483–500`, `continuation_driver.py:1418–1434`; actual app/worker `test_continuation_control_successor.py:94–191` |
| No real automatic successor | `test_continuation_action_fit_successor.py:232–311` exercises sequence 2→3 and no paid replay; G01 is narrower |
| Initial queued/interrupted segment notice missing | `ComposingIndicator.tsx:106–120` and explicit continuation component tests |
| U17 dossier carry missing | All eight test files carried; wire-secret donor blob `dff7157cb80705b9789d5a1c05ae75c554745a55` exact, all six previously missing functions present |
| PromptShield exact-limit public-route test missing | `tests/unit/web/sessions/test_prompt_guard_document_limit_route.py:399`, actual POST/settle at 10000/+1 |
| PromptShield PostgreSQL audit ordering/failure/cancellation missing | `tests/testcontainer/web/test_prompt_guard_admission_postgres.py:115,173,231`, independent audit read, real rejecting trigger and cancellation join |
| Recovery/process drain only isolated definitions | `tests/testcontainer/web/test_continuation_recovery_process_postgres.py:964–1178`, actual server/signal/HTTP/peer retained and uncertain cases |
| Proof marker inventory mismatch | Exactly three current marked proof functions match `scripts/ci_test_shards.py`; G02 concerns source guards instead |

Do not reintroduce historical V1/V2 lifecycle machinery, obsolete planner paths,
or the dropped gateway worker pair to "finish" these units. Each was superseded
or deliberately retired. Under the 6 November ruling, obsolete compatibility
paths should go as callers consolidate; current dependency format tags must not
be removed solely because they look like public lifecycle versions.
