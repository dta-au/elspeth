# Quota remediation

This review covers the unreleased 0.8.9 candidate on
`codex/quota-remediation-20261019`, based on
`f4c2f88497f7105afc114715e5072aef66d3d37c`. It implements the retained
Q1–Q14 and S1–S3 quota audit items, with the Q12 reachability correction below.
It also implements the previously disclosed inert container-storage setting.

## Changes and evidence

| Audit item | Result | Focused regression surface |
| --- | --- | --- |
| Q1: run recovery | Sessions admission identity is the durable logical Landscape call identity. Recovery settles exact durable evidence under a fresh execution fence; absent outcomes remain pending and unknown usage remains unknown. | `test_run_crash_accounting_recovery.py`, `test_run_provider_bridge.py` |
| Q2 and L1: shared storage | Startup provisions the configured container storage ceiling, including storage-only policy. Admission compares personal and aggregate deployment usage independently under policy locks. | `test_container_quota_policy.py`, `test_storage_quota_authority.py`, PostgreSQL quota authority tests |
| Q3: output writes | File sinks reserve before each write and account for retained targets and stages. Crash reconciliation preserves existing-byte charges without authorizing new growth. Web preflight refuses unmetered local SQLite sinks and persistent Chroma sinks, retrieval and collection probes before construction. | `test_output_storage_admission.py`, `test_output_archive_accounting.py`, `test_local_database_sink_quota_admission.py` |
| Q4: clock authority | Composer and EXECUTE bind monotonic elapsed duration to database admission time. The first terminal timestamp is retained; outcome stamping needs no post-spend Sessions read. Recovery reuses the durable timestamp. | Provider quota end-to-end, run clock and crash-accounting tests, including skewed host clocks |
| Q5: definitive refusals | Structured application rate-limit and queue-full responses terminate submission and display retry guidance. Ambiguous gateway responses retain recovery behavior. | Composer observer and client recovery tests |
| Q6 and Q7: policy state | Status distinguishes active, never-issued and suspended policies. Persisted policies remain editable after defaults are removed; suspension and restoration use the audited authority. | Quota routes, policy audit, editor and People dialog tests |
| Q8: exact quantities | Full signed-64-bit quota quantities use decimal strings over HTTP and BigInt validation in the browser. Legacy safe integers remain accepted; unsafe numeric requests are refused. | Quota routes, editor, workflow and storage parser tests |
| Q9: rate-policy drift | Durable buckets bind the request limit. Conflicting replica limits fail closed until the bucket expires. | Rate policy binding and PostgreSQL cross-process tests |
| Q10: proxy attribution | Trusted forwarding peers are explicit validated IPs/CIDRs; the default trusts none. Compose/nginx supplies an explicit gateway and overwrites incoming forwarding headers. | Trusted proxy, CLI and deployment-template tests |
| Q11: fork outage | Storage-accounting unavailability retains its typed 503 classification through durable fork failure and replay. | Fork and operation receipt tests |
| Q12: dispatch cancellation | Explicit custody remains mandatory for physical Composer dispatch. Pre-dispatch callback refusal settles proven-undispatched work. The alleged live legacy trigger was already blocked by the prompt guard; its dormant fallback was removed. | Explicit custody and provider quota end-to-end tests |
| Q13: model provenance | Landscape persists nullable `returned_model` independently of response retention; run accounting prefers it over configuration. | Call persistence, strict loader, provider and recovery tests |
| Q14: shared status | Status exposes aggregate container token/storage usage separately from personal usage, with accounting-known state. | Quota route and editor tests |
| S1–S3: persistence integrity | Schema rejects negative token quantities and binds settled attempts to owner/session/source/run provenance. Settlement and bulk replay verify source/run identity. | Quota integrity schema, epoch preserve/refuse and PostgreSQL integrity tests |

## Review repairs

The first independent Astra review required three further repairs:

1. Output-blob deletion and shrinking replacement now reconcile reservations
   after physical cleanup succeeds. Interrupted cleanup retains the charge for
   recovery. This lifecycle is supported by the service, but ordinary current
   Web execution produces generic artifacts rather than creating output blobs;
   the regression fixture supplies the output-blob producer.
2. Persistent Chroma bypassed the file-writer hook. The Web preflight refusal
   now also covers persistent retrieval/probes and local SQLite database sinks.
   Remote Chroma/PostgreSQL and standalone CLI construction have positive
   controls.
3. Storage refusals carry the violated scope and its exact limit. Shared usage
   is no longer presented against a personal denominator; old unscoped errors
   retain generic guidance.

PostgreSQL integration additionally exposed `SUM(BIGINT)` returning Decimal.
Accounting now accepts finite integral Decimal aggregates exactly and rejects
fractional, non-finite and unsupported values.

The second Astra review closed R1–R3 and identified R4: the initial EXECUTE
clock repair queried Sessions after provider spend but before recording the
Landscape outcome. A controlled accounting-store outage left no durable known
outcome, despite Landscape remaining writable. EXECUTE now binds its committed
admission timestamp to monotonic time before dispatch, caches the first terminal
stamp, and records without that extra Sessions dependency. Pre-dispatch,
Landscape custody and after-call settlement checks remain in place. Fresh
recovery uses durable evidence rather than the in-memory clock binding.

The third independent Astra review found no remaining concrete findings in the
agreed quota scope and closed R1–R4. It independently ran the final clock,
post-spend outage, file-backed restart and provider-bridge selection:
**21 passed, exit 0**. Earlier review reports preserve their historical findings;
the final implementation and review supersede the interim post-spend clock
lookup. Full reports are `astra-review-1.md`, `astra-review-2.md` and
`astra-review-3.md` in the lane directory.

## Compatibility and operational boundaries

- Sessions schema epoch is **78**; Landscape schema epoch is **50**. Existing
  predecessor stores are preserved and refused. No migration, reset or data
  deletion was performed by this work.
- Quota status and refusal quantities use decimal strings. Closed-shape clients
  must accept the new policy-state, shared-usage and refusal-scope fields.
  Landscape call exports add nullable `returned_model`.
- Existing Web graphs selecting persistent Chroma or local SQLite output need a
  supported remote or file target. Standalone CLI behavior is unchanged. Remote
  modes still require ordinary plugin authorization and security admission.
- Retained generic outputs and admitted stage reservations consume storage.
  Archiving a run-backed session retains artifacts and their charge. The file
  manager deletes uploads; there is no generic artifact-purge control. Capacity
  or retention changes require an operator decision.
- Token caps remain measured next-call cutoffs and may overshoot during an
  admitted call. Composer queue capacity remains a soft cap. Policy audit and
  Sessions mutation are not a distributed atomic transaction. Those accepted
  limitations were not redesigned.

## Validation boundary

Validation is focused on quota behavior and affected contracts. Broad baseline
suite failures are outside this task. Tests use both worktree import roots and
the existing environment; no dependency install or environment rebuild was
performed. Lane-private reports and logs are under
`.claude/lanes/quota-remediation-20261019/`.

Completed evidence, each with an observed process exit code of zero:

| Selection | Result | Lane log |
| --- | --- | --- |
| Integrated quota authority, policy, storage, schema, run recovery, local-store preflight and proxy regressions | 248 passed | `final-quota.log` |
| Focused frontend quota, editor, People dialog, storage and submission recovery | 124 passed in 10 files | `final-frontend.log` |
| PostgreSQL quota integrity, authority, policy width, rate binding and Composer atomic admission | 52 passed; relevant scope changed concurrently, so followed by the frozen repeat below | `focused-pg-final.log` |
| Final PostgreSQL storage/integrity scope, including observed lock contention and cross-identity reservation | 12 passed; affected files unchanged during run | `focused-pg-storage-final.log`, `focused-pg-storage-frozen.log` |
| Provider quota/custody/guard contracts | 105 passed, 5 deselected | `provider-verified-tests.log` |
| Run observation/recovery/export contracts | 124 passed | `run-current.log` |
| Final run clock, post-spend outage, known/unknown outcomes and fresh-service replay | 15 passed | `run-clock-repair.log` |
| Astra independent final clock/recovery/provider-bridge selection | 21 passed | `astra-round3-focused.log`, `astra-round3-focused.exit` |
| Policy audit and token adapters | 102 passed | `audit-final2.log` |
| Documentation/site compatibility contracts | 34 passed | `docs-review-checks.log`, `docs-review-guide-checks.log` |
| Source typing | Passed | `final-mypy.log` |
| Frontend typecheck, Ruff, formatting and configuration contracts | Passed | `final-typecheck.log`, `final-ruff.log`, `final-format.log`, `final-contracts.log` |

Selections overlap; their counts are not a unique-test total. The provider
selection excludes four title transport cases and one reasoning-artifact
expectation that also failed on the untouched baseline. An earlier full
frontend run had two registry-projector subprocess failures (`spawnSync EPERM`)
and 4388 passing tests; no unrelated repair or full-suite clearance is claimed.
Broad static writer/masquerade investigations were interrupted and provide no
completed gate result.

The final 15-case lane selection and Astra's 21-case selection follow the R4
repair. The larger integrated and run selections preceded R4; the final review
retested its affected clock, admission, audit and recovery paths. The final UI
storage-usage wording also passed its three component tests after the 124-test
frontend selection (`policy-usage-copy.log`). The earlier
`run-clock-final.log` documents the superseded clock implementation and is not
used as final R4 evidence.

No live provider acceptance, multi-replica deployment acceptance, actual
process-kill test, disk-exhaustion injection, operator signing, merge, push or
deployment is claimed.
