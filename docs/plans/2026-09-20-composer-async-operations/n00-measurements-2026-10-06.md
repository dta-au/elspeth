# N00 current-source measurements — 2026-10-06

**Readiness evidence, not implementation clearance.** These measurements use current HEAD `edc844699a350a90a624089e12a5a1e75b7b2dde`, Python 3.13.15 and both held-worktree source roots. Application, tests, scripts, gateway and lockfiles remain unchanged. Fake-provider tests, ephemeral loopback HTTP and a fresh local PostgreSQL container were used. No production or paid-provider call, application implementation, review transmission, commit or merge occurred. The original two-review custody gap, product decision and actual approved Daybreak Blue review remain open.

Full path-normalized baseline corpora are retained in [evidence](evidence-2026-10-06/README.md), with every finding/site row preserved. Raw logs, explicit exit files, full native diagnostic JSON and diagnostic scripts remain in `<consumer-workspace>/readiness-baseline/` and its parent. Failed imports, incorrect fixture assumptions and negative controls are retained. No real user message, provider credential, production upstream content or operator HMAC key was acquired for this report. The task-local observer records metadata only; it adds no service reads.

## Completed runner evidence

| Work | Explicit exit | Completed output / artifact |
|---|---|---|
| Existing fake-provider route/auto-title selection, serial | 0 | `160 passed, 256 deselected, 1 warning in 41.84s`; `observed-turn-baseline.log` |
| Controlled provider-entered hot-title cases, serial | 0 | `2 passed, 2 warnings in 4.73s`; `hot-title-provider-entered.log` |
| Actual LiteLLM against ephemeral loopback gateway | 0 | `gateway-timeout-classification.json`; success/504/503 controls, exactly one call per case |
| Accepted DTO byte bounds + baseline receipt vectors | 0 | `wire-bounds-and-extended-vectors.json` |
| Serial local PostgreSQL cascade + actual FK controls | 0 | `pg-fk-instrument-controlled.exit`, `postgres-cascade-control.json`, `postgres-restrict-instrument-control.json` |
| Actual production exception-handler registration and direct route exits | 0 | `registered-handler-and-route-exits.json` |
| Complete canonical writer corpus, including scanner controls | 0 | `writer-full-corpus.json`; existing drift retained in full |
| Keyless all-rules source lint | 1 | Existing diagnostic corpus; `keyless-current-source.json/.stderr/.exit`, normalized full corpus and summary |

No broad suite was launched alongside the other owner's active CI shards. These selections do not test the future job/stream code or establish full-suite, deployed transport or operator signature clearance.

## M3/M4: complete baseline corpora

Native keyless output, measured directly rather than inferred from historical producer logs:

```json
{
  "exit": 1,
  "diagnostics": 2609,
  "unique_full_rows": 2609,
  "by_rule": {
    "allowlist.unused_rule": 3,
    "R6": 152,
    "R5": 471,
    "R1": 57,
    "R9": 6,
    "R4": 23,
    "R8": 9,
    "R2": 24,
    "R7": 8,
    "L1": 1,
    "R_TB_SUPPRESSED": 1548,
    "trust_tier.tier_model": 307
  },
  "by_severity": {
    "error": 1061,
    "note": 1548
  },
  "stderr_lines": 118,
  "controls": "known first diagnostic validated; modified first fingerprint changes full-row set",
  "signature_clearance": false
}
```

The 118 stderr lines retain stale deleted-file allowlist warnings. Full diagnostics include suppressed notes. A known first diagnostic and changed-fingerprint full-set control verified the normalization instrument. This is shape-only verification when the operator key is missing, not signature validity. No suppressions, signatures or allowlist entries were changed.

Canonical writer scanner output (all sites and every category retained, including all 45 unresolved executions rather than the xfail display's first 40):

```json
{
  "counts": {
    "unexpected": 76,
    "stale_reviewed": 0,
    "connection_violations": 26,
    "unresolved": 45,
    "unclassified": 5,
    "stale_read": 5,
    "stale_non_session": 0,
    "invalid_reviewed_read": 0
  },
  "authority_mismatches": 0,
  "scanned": 484,
  "live": 356
}
```

The existing architecture test remains an expected failure, not a clean inventory. Scanner controls first scan a harmless function with no sites, then mutate it to an actual `chat_messages.insert` and require detection. Source before/after comparison must use these corpora after dependency changes and implementation, not compare to zero.

## M6/M7: wire bytes and current caller evidence

Actual ASGI metadata from the 160-test selection observed 92 authoring POSTs. The plugin's collection list contains deselected tests and is **not** an executed-test inventory. The captured turns and completed pytest result are the execution evidence. Seventeen turns have no actual `get_current_state` observation; do not infer their heads. Some tests intentionally raise before HTTP completion, leaving status unset and zero response bytes.

Measured nearest-rank `ceil(p*n)` quantiles, including all observed statuses, follow. A fixed four-element p50/p99 positive control verifies the quantile function. These small fake-provider fixtures are wire proxies, not representative production traffic and not persisted `request_json`/`result_json` distributions. The future operation table does not exist; actual column distributions and strict future DTO overhead must be measured in N03/N04/N17.

```json
{
  "messages": {
    "count": 43,
    "statuses": {
      "200": 25,
      "500": 4,
      "502": 3,
      "422": 6,
      "None": 5
    },
    "request_bytes": {
      "p50": 90,
      "p99": 141,
      "max": 141
    },
    "result_bytes": {
      "p50": 444,
      "p99": 3161,
      "max": 3161
    }
  },
  "recompose": {
    "count": 49,
    "statuses": {
      "200": 9,
      "502": 8,
      "504": 2,
      "503": 1,
      "500": 8,
      "422": 8,
      "None": 12,
      "409": 1
    },
    "request_bytes": {
      "p50": 67,
      "p99": 67,
      "max": 67
    },
    "result_bytes": {
      "p50": 170,
      "p99": 3181,
      "max": 3181
    }
  }
}
```

Actual successful head-present/state-omitted callers:

```json
[
  {
    "nodeid": "tests/unit/web/sessions/test_routes.py::TestMessageRoutes::test_send_message_persists_llm_call_audit_sidecars_with_precompose_state",
    "path_kind": "messages",
    "status": 200,
    "state_field": "omitted",
    "head_observations": [
      true
    ]
  },
  {
    "nodeid": "tests/unit/web/sessions/test_routes.py::test_send_message_state_advance_preserves_existing_composer_meta",
    "path_kind": "messages",
    "status": 200,
    "state_field": "omitted",
    "head_observations": [
      true
    ]
  },
  {
    "nodeid": "tests/unit/web/sessions/test_routes.py::test_recompose_state_advance_preserves_existing_composer_meta",
    "path_kind": "recompose",
    "status": 200,
    "state_field": "omitted",
    "head_observations": [
      true
    ]
  }
]
```

Named source caller dispositions from current producer code:

| Producer | Current behavior | Cutover owner |
|---|---|---|
| `frontend/src/api/client.ts::sendMessage` | Optional state parameter: undefined omits, null is explicit. `sessionStore.sendMessage` supplies current state ID or null; retry supplies retained requested state or null. | N03/N14/N15 require explicit admitted base and immutable exact operation body; retain retry custody. |
| `frontend/src/api/client.ts::recompose` / store retry | Sends only expected user-message ID today, even when a head exists. | N03/N06/N14/N15 add the strict operation/base contract and authoritative outcome recovery. |
| `scripts/composer_acceptance/runner.py::compose` | Submits `{content}` and polls session progress; later turns may have a head. No state or retry identity in its current request. | N14: GET the actual head and submit the reviewed explicit-base/id contract; follow operation progress/terminal. No production execution here. |
| `evals/composer-battery/drive_battery.py::run_prompt` | Initial fresh-session POST sends content + client_request_id, omits state; title/preferences preparation and later GET settlement are separate. | N14: retain initial base-null semantics, then operation-scoped observation and terminal; update stale unaudited-title comment when touching it. No battery/provider execution here. |

This records the current producer surfaces and runtime omitted-head witnesses; it does not claim every repository test or dynamically constructed external caller was executed. N14 needs the full compatibility/caller matrix on the implementation, including script/eval mocks and frontend generations.

The unmodified `SendMessageRequest.content` cap is **65,536 characters**, not an encoded-byte cap. Accepted maximum examples:

| Fixture | UTF-8 content bytes | Serialized JSON UTF-8 bytes |
|---|---:|---:|
| ASCII | 65,536 | 65,625 |
| Emoji | 262,144 | 262,233 |
| Quotes or backslashes | 65,536 | 131,161 |
| Visible character + NULs | 65,536 | 393,300 |
| Visible character + newlines | 65,536 | 131,160 |

The 65,537-character negative control is rejected by the real DTO. ASCII fits the old 256 KiB proposal; emoji and escaped controls do not. Revise the proposal to **512 KiB per submitted browser body and 1 MiB aggregate per principal/provider**, 24 h retention, without evicting unresolved custody to admit another action. This is measured headroom for current fixtures, still unapproved and not proof that all future wrappers/DTOs fit. Measure actual encoded storage and future canonical request/result columns before accepting final caps. SQL text `length()` measures characters; an encoded-byte requirement needs application UTF-8 validation and dialect-correct SQL (`octet_length` in PostgreSQL, blob byte length in SQLite), reviewed together.

## M8/M12: actual hot auto-title ordering

Two initial fixture runs returned early at provider admission and are retained as refusal-path evidence. A required fake-provider-entry assertion made that false timing instrument fail. The observer captured `ChargeableAdmissionRefused` / `user_role_required` in the private replacement database. The corrected fixture uses the unchanged route fixture's existing role/policy setup with a file database; it adds no live grant. Both final cases enter the local fake provider and return the same completed assistant reply.

| Delay fixture | Observed ordering, seconds after request |
|---|---|
| 0.05 s | Assistant service completion 0.019458; ledger insert 0.074268 and attempt settlement 0.074567; title SQL update 0.076003; HTTP start 0.077565. |
| 2.1 s | Assistant service completion 0.014072; **provider admission SQL insert 0.019366**; cancellation settlement ledger insert 2.020006 and attempt update 2.020335; HTTP start 2.021809; no title update. |

Full ordered events and service failures remain in `observed-hot-title-provider-entered.json` and `hot-title-timing-summary.json`. Compiled DML instrumentation excludes raw SQL/triggers and is not a complete authority inventory. The 160-run observer originally nested `_run_sync` wrappers, so its SQL method attribution cannot be used as writer classification; the corrected hot-title run preserves it.

Current code has no durable job terminal. The trace proves that assistant persistence alone is not turn completion: later ledger admission, settlement and title mutations exist. N07/N09 must join admission/provider/cancellation/accounting/title work before the atomic future terminal. Do not relabel `begin_provider_attempt` or `update_session_title` audit-only.

Current-source semantic classifications remain concrete review input: `_persist_llm_calls`, `_persist_turn_audit_cohort` and planner audit evidence record attempts/results; `finish_provider_attempt` adds terminal audit drafts; `settle_provider_attempt` updates attempt and token ledger; `cancel_undispatched_provider_attempt` appends audited no-dispatch closure and settles the same attempt. Associated token recording/session timestamp mutation may be audit-only **only inside that verified cohort**. Provider admission and title changes are ordinary fenced writes. Current production settlement still requires the original live fence; proposed audit-only positive-predicate exceptions do not yet exist. N07 must prove the exact method/transaction call sites and post-terminal refusal/settlement invariants on the implemented tree, against the retained full writer baseline.

## M9: gateway classification and configuration boundary

The probe invokes actual `_litellm_acompletion`, LiteLLM/OpenAI SDK, gateway `error_envelope` and production failure classifier against a temporary **127.0.0.1** HTTP service with explicit fake key/base and zero retries. Success admits a complete model response. Gateway 504 `UPSTREAM_TIMEOUT` becomes `litellm.exceptions.Timeout` / status 504, classified `timeout`, audit status `timeout`, retryable false. Gateway 503 becomes `ServiceUnavailableError`, classified `unavailable`, audit status `api_error`, retryable true. Bare lifecycle `TimeoutError` is not classified as SDK weather. The server observes exactly success/timeout/unavailable, with no retry. Existing route tests also exercise HTTP 504; `_handle_composer_provider_failure` maps SDK timeout to 504 `llm_unavailable` with safe gateway guidance, distinct from convergence timeout 422.

Parsed shipped compose service mappings contain `elspeth/postgres/postgres-bootstrap` and `state-init/web/web-init`; neither declares a service named gateway. Actual `WebSettings` default primary endpoint is null; `GatewayConfig` timeout default is 300 s. Controls verify the known postgres and absence of a made-up service. **These declarations do not establish deployed sidecar presence or deployment timeout.** No production settings/process query is authorized here. Preserve this operator-configuration gate; select and verify the actual supported local acceptance target before TLS tests.

Current gateway request validation forbids `stream`; provider admission accepts complete responses. Generated answer deltas therefore still need the product decision and a supported provider/gateway/audit design. Common durable/auth/observer readiness can proceed independently.

## M10: unmodified codec vectors

Original production fork/revert request and fork response vectors remain in `receipt-vectors.json`. Extended strict task-local fixture vectors exercise omitted/default/null parity, different retry-ID equality and payload-change inequality using the unmodified receipt codec:

```text
state_revert default/null fixture: 4c6e631376994d5777cdf175e272a02a0ecbf394de2087583c508fdd6f98bf06
session_fork default/null fixture: 40ac0499de2cc2c483cf6659131400b57cb9129fbaa37bfc018ff6ebcf896152
strict default/null response fixture: 6ecb00e57bddbb124ed080a0acc654a1ec39b59a48d3565a68ca96ca23c4f6fa
```

These are not composer DTOs. Extraction byte parity and future composer golden fixtures cannot be measured before the extracted implementation/strict DTOs exist. Keep this as an N02/N03 implementation gate, not an unanswered-product-choice blocker.

## M11: serial local PostgreSQL cascade and controls

Canonical `postgres_test_target`, cached `postgres:16-alpine`, a fresh private container (256 MiB / half CPU), local Unix Docker socket and removed external test URL provided the target. Context cleanup disposes it. Production schema and owned service/lease create one real user message and ingress receipt; an isolated sibling table adds session CASCADE, user-message NO ACTION or RESTRICT, actor RESTRICT.

Each case's actual before query yields one message, one ingress receipt and one sibling. **Session deletion cascades for both NO ACTION and RESTRICT**, with all three after counts zero. This probe does not reproduce the earlier RESTRICT hypothesis; the initially assumed failure assertion is retained as a failed diagnostic, not evidence supporting that hypothesis. Direct production message deletion is refused with SQLSTATE 23000, a trigger guard rather than FK violation. Separate known live RESTRICT control rejects parent deletion with SQLSTATE 23503 / `readiness_control_child_parent_id_fkey`; removing the child then permits parent deletion (after count zero). Thus the instrument actually detects a restrictive FK rather than interpreting every integrity error as one.

This isolated sibling does not reproduce the proposed composite job/ingress FK set, all final triggers, archive semantics or contention. Keep N04/N17's final schema/cascade proof and earlier concern; do not erase it or assert RESTRICT necessarily fails. The prior NO ACTION proposal remains subject to final-schema review.

## Appendix A and residual gates

[Appendix A](appendix-a-2026-10-06.md) records actual production registration, every direct route return/raise and handler return/raise, with owning tasks. These are bounded live-source baseline artifacts, not proof that all transitive helper/storage/provider failures have stable future public envelopes. N10/N11a/N11b must freeze that matrix after extraction and test actual normalization/persistence/replay; unknown internal errors remain safe generic failed terminals.

| Gate type | Concrete residual work |
|---|---|
| Product-decision dependent | Provisional answer text versus safe progress + completed answer; answer-delta withholding/redaction, provider/gateway stream, attempt/audit/usage and reconnect policy if selected. |
| Prior finding custody / reviewer authorization | Recover both original reports and map every finding; approve exact review payload/destination under applicable policy; invoke actual `gpt-daybreak-blue-latest` and resolve its plan blockers. Prior archive payload denial remains separate and unresolved. |
| Implementation dependent | Actual operation request/result column distributions, strict composer codec parity, complete caller compatibility tests, exact audit-only predicate inventory, job terminal ordering, final composite PostgreSQL schema/cascade/contention, error normalization/settlement/replay and real cancellation/thread completion. No application code is authorized yet. |
| Local transport acceptance dependent | Implemented authenticated SSE with revocation, actual send/read/driver/occupancy bounds, reader accounting, decoder limits, buffered proxy fallback and isolated HTTPS early chunk tests. Current proposed timers/caps do not prove viability. |
| Main-bound ownership / deployed configuration | Consume landed PR275/276 rather than duplicate; refresh provenance/corpora on exact resulting main. Deployed sidecar presence is unmeasured; no production query. Retry artifact remains exact and unapplied. |
| Delivery | Hold reviewed branch for John's local tests. No merge, deploy, live grants, production or paid-provider tests. |

No remaining baseline in this report is labelled blocked solely by the product choice. Future behavior proofs are explicitly separated from measurements now completed. A complete implementation-clearance claim still requires every residual gate above.

Final [source/document validation](evidence-2026-10-06/final-n00-validation.json) exited 0: all tracked non-document inputs match HEAD, changes remain planning-only, main retains only untracked BUGREP.md, active links/history controls pass, and complete lint/writer corpora remain preserved. Final fetch and exact unapplied retry patch check also exit 0; PR275/276 remain independent open drafts.
