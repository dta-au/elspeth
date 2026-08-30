# Tier-remediation dispositions — lane web-sessions

Lane ticket elspeth-be56da8b13 (epic elspeth-e561df3c4e), branch
`lane/tier-rem-web-sessions`. Covers all 66 blocked findings of bundle
`sign-2026-08-30-w1` under `src/elspeth/web/sessions/`.

**Pairing caveat (applies throughout).** The bundle's `judge_rationale`
texts were heuristically paired with finding keys and are scrambled in this
lane — many rationales describe a *different* site than their key's
function tag (e.g. rationales keyed to `guided_chat_atomic.py` adjudicate
`guided_plan.py` handlers, and several `service.py` keys carry stale
function tags from before line drift). Every disposition below was
re-derived from the code; each entry names the **actual site** adjudicated.
The hub should measure by corpus diff, not by key→line arithmetic.

**Measurement (raw tier_model corpus, `R_TB_SUPPRESSED` and stale-entry
lines excluded, other lanes' files excluded):** 65 findings before → 56
after, with 31 sites now structurally covered by `@trust_boundary`
(`R_TB_SUPPRESSED`) in `web/sessions/`. Every surviving site is listed
below as a restage with proposed rationale text.

**Commits** (all on `lane/tier-rem-web-sessions`):

| sha | scope |
|---|---|
| c3dac430a | emitters.py — required KnobField `tier` on synthetic knob (pre-existing mypy blocker, see § Out-of-scope fix) |
| 61e561061 | `_helpers.py` — Tier-1 tool-row reads, 2 trust boundaries, `_log_last_resort_diagnostic`, recorded watcher degradation |
| c489d6146 | `guided_audit.py` — recognizer stops swallowing TypeError |
| 086f442c8 | `guided.py` — bounded START rejoin, exact-dict turn reads, RESPOND body boundary, log-suppress conversions |
| 9532622d9 | `guided_chat_atomic.py` — settlement failures surface, prefill exact-dict, recorded progress failure |
| 69765dcc5 | `guided_plan.py` — plugin-crash classification, settlement-failure crashes, cancel-aware progress publisher (+ 2 integration tests re-pinned) |
| 83b4f56e4 | `sessions.py` + `schema.py` — bounded fork rejoin, recorded blob-cleanup residue, honest schema probe |
| b28e70b65 | `service.py` — strict interpretation parsing, 4 trust boundaries, fork-plan/projection/rmdir honesty (+ 2 new test files) |

Disposition legend: **fixed** — code change retires the finding;
**fixed+restage** — the judged defect is fixed but a (narrower, now-honest)
site survives under a new fingerprint and needs a fresh per-line entry;
**restage** — code is right, per-line entry is the honest form, proposed
rationale included.

---

## web/sessions/_auto_title.py

### `R6:maybe_auto_title_session:fp=819a9041915266f8` — restage
Stale drift entry (the except tuple gained `_MalformedAutoTitleResponseError`
after signing). Code is the accepted record-and-surface form: a narrow,
enumerated set of expected provider/scheduling failures
(`LiteLLMAPIError`, `TimeoutError`, `asyncio.CancelledError`,
`_MalformedAutoTitleResponseError`), each recorded on the OTel failure
counter with an exception-class label before returning the declared
best-effort result. Hub: delete entry `819a…`, stage fresh.

Proposed rationale: R6 fires on the terminal handler of best-effort
first-message auto-titling (`_auto_title.py:maybe_auto_title_session`).
The catch is a closed tuple of *expected* external failures from the
Tier-3 LLM naming call plus the awaiting caller's timeout-cancel:
`LiteLLMAPIError` (provider), `TimeoutError`, `asyncio.CancelledError`
(the spawner awaits the task with a timeout in `routes/messages.py`
send_message; cancellation is an expected scheduling outcome, not a lost
signal), and `_MalformedAutoTitleResponseError` (this module's own typed
rejection of a malformed completion). Every arm is RECORDED —
`_record_auto_title_failure(exc)` increments
`composer.auto_title.failed` labelled by exception class — and the
declared result ("no auto title; session keeps its minted default") is
returned explicitly. Programmer bugs and `update_session_title` DB
failures are outside the tuple and propagate to the awaiting caller.

## web/sessions/guided_audit.py

### `R6:_is_canonical_uuid:fp=0d974040b7413a6d` — restage
`ValueError` from `UUID(value)` is CPython's documented "not a UUID"
signal; returning `False` is the predicate's declared verdict, not a lost
failure. The verdict is enforced fail-closed downstream: a `False` from
`_valid_guided_synthetic_payload` makes
`is_authentic_guided_synthetic_invocation` return `False`, and its sole
caller (`service._require_exact_guided_intent_cancellation_audit`,
service.py:960) raises `AuditIntegrityError` on that verdict.

Proposed rationale: `_is_canonical_uuid` is a pure recognizer over a
string extracted from composer-lineage synthetic-event arguments; the
`except ValueError: return False` converts the interpreter's typed
not-a-UUID signal into the boolean answer the authenticity check asks
for. Nothing is discarded: the False verdict flows into
`is_authentic_guided_synthetic_invocation`, whose only caller raises
`AuditIntegrityError` when the event is not authentic
(`web/sessions/service.py::_require_exact_guided_intent_cancellation_audit`).
Pinned by `tests/unit/web/sessions/test_guided_atomic_settlement.py`
(intent-cancellation audit tests exercise the raise arm).

### `R6:is_authentic_guided_synthetic_invocation:fp=b3e4f8672337d90e` — fixed+restage (c489d6146)
The old `except (TypeError, ValueError)` swallowed `TypeError` — a
wrong-typed `arguments_canonical` on the owned `ComposerToolInvocation`
contract would have masqueraded as "inauthentic". Narrowed to
`json.JSONDecodeError`; a TypeError now propagates as first-party
corruption. The residual narrowed catch is the recognizer's negative
verdict, enforced fail-closed at service.py:960 (see above); stage it
with that rationale (a comment at the site records the same reasoning).

## web/sessions/routes/_helpers.py

### `R1:_extract_runtime_model_snapshot:fp=dc84631f2ee5050f` — fixed (61e561061)
Converted to the structural form the judge prescribes for R1/R5 rooted at
an external parameter: `@trust_boundary(tier=3, source_param="state",
suppresses=("R1","R5"))`, raising `AuditIntegrityError` on malformed
composer-authored option shapes, absent optional pins projecting as
`None`. Pinning test
`tests/unit/web/sessions/routes/test_trust_boundary_helpers.py::test_extract_runtime_model_snapshot_rejects_non_string_model`
(+ non-mapping-options and absent-pin cases), AST fingerprint recorded in
the metadata. Hub: delete entries `dc84…`, `e29c…` (R1) and `ec9f…` (R5)
for this function.

### `R5:_guided_source_commit_failure_detail:fp=c2e307ce9f51d64a` and `fp=de0839b7c3868a67` — fixed (61e561061)
Same structural conversion: `@trust_boundary(tier=3,
source_param="tool_result", suppresses=("R1","R5"))` on the egress
sanitizer — `ToolResult.data` is plugin/tool-produced Tier-3 content and
this function is its declared parse boundary (raises TypeError on a
non-ToolResult carrier; any unrecognized data shape yields the closed
generic detail, never a raw repr). Pinning test
`…::test_guided_source_commit_failure_detail_rejects_non_tool_result`.
Hub: delete entries `c2e3…`, `de08…` (R5) and `968e…` (R1).

### `R5:_tool_call_outcomes_by_call_id:fp=823e6a3e591eaa52` — fixed (61e561061)
The judged defect class (defensive re-check of a first-party fixed
contract) held: the audit envelope `{"_kind": "audit", "invocation": {…}}`
is written only by `_persist_tool_invocations`, and a DB round-trip does
not demote authorship. The `isinstance(invocation, Mapping) else {}`
default silently reclassified a corrupted row; replaced with a direct
Tier-1 read (`envelope["invocation"]`) that crashes on corruption.

### `R6:_tool_call_outcomes_by_call_id:fp=8165532fbd73d589` — fixed (61e561061)
`ChatMessageRecord.content` is non-nullable `str` (TypeError could never
fire honestly) and both tool-row writers persist JSON, so the
`except (TypeError, ValueError): content = None` default fabricated a
COMPLETED classification from corruption. Undecodable tool-row content now
raises `AuditIntegrityError`. Test
`test_tool_call_outcomes.py::test_undecodable_content_is_tier1_corruption`
replaces the old default-pinning test.

### `R4:_cancel_on_client_disconnect:_watch_disconnect:fp=a26356d9b433ceaf` — fixed+restage (61e561061)
The broad catch around `await request.receive()` is a genuine third-party
ASGI-transport boundary (uvicorn/h11 raise unenumerable types on a broken
channel), but it was SILENT — a dead watcher re-opens the zombie-compose
window (elspeth-e08063c3a5) indistinguishably from "no disconnect ever
arrived". The degradation is now recorded
(`compose.disconnect_watcher_receive_failed`) via
`_log_last_resort_diagnostic` before the declared stop-watching return.

Proposed rationale for the residual R4: broad catch at a third-party ASGI
transport read; the failure class is unenumerable (server-internal
transport errors), `asyncio.CancelledError` is not caught (BaseException),
the degraded outcome (stop watching, never cancel a healthy compose on a
transport quirk) is the declared result, and the event is recorded on the
last-resort channel before returning.

### `R7:_cancel_on_client_disconnect:fp=432195feedd58109` — restage
`with contextlib.suppress(asyncio.CancelledError): await watcher` is the
reap-the-cancelled-child idiom, and the doctrine's condition ("an active
CancelledError is provably re-raised on every path") holds in the visible
code: immediately below, `if task.cancelling() > 0` re-checks the
enclosing task and either flushes a *disconnect*-cancel deliberately
(documented semantics: compose completed, response discarded by the dead
transport) or re-raises `asyncio.CancelledError()` for an external cancel.

Proposed rationale: the suppress absorbs only the watcher child's own
cancellation echo when reaping it on the normal-exit path. An external
cancellation delivered at this await is not lost: the `task.cancelling()`
re-check directly below re-raises `CancelledError` whenever a cancel
request remains after the disconnect-flush bookkeeping, and the
disconnect-flush arm itself is the function's documented contract
(elspeth-e08063c3a5). Pinned by
`tests/unit/web/sessions/test_routes.py::…::test_client_disconnect_cancels_compose_turn`
and `…::test_external_cancel_racing_disconnect_keeps_unwinding` (the
external-cancel re-raise arm).

## web/sessions/routes/composer/guided.py

### `R4:post_guided_convert:fp=38e1c6402b0d1947` — fixed+restage (086f442c8)
The validated rationale on this key adjudicates the guided START
`while True` fence-loss loop ("no fall-through, can repeat
indefinitely") — that defect is fixed: the loop is now bounded
(`_GUIDED_FENCE_REJOIN_ATTEMPTS = 5`) and terminates in
`AuditIntegrityError("Guided START lost its operation fence on every
rejoin attempt…")`. The key's own site (post_guided_convert's
`except Exception as exc:` arm) is the prescribed compensating broad
catch: classify → `slog.error` for integrity classes → durable
`fail_guided_operation` → `raise_guided_operation_failure(failed)`
(typed `-> Never`, verified at routes/guided_operations.py:145).

Proposed rationale for the convert arm: broad catch that terminates on
every path — the failure is classified into the closed
`GuidedOperationFailureCode` vocabulary, durably recorded via
`fail_guided_operation`, and surfaced through
`raise_guided_operation_failure`, which is typed `-> Never` and raises
from a closed failure table; a fence loss during the failure write
propagates (another worker owns settlement). Nothing returns normally.

### `R4:post_guided_start:fp=6989b1dc6a4e7125` — fixed+restage (086f442c8)
The validated rationale on this key adjudicates the
`suppress(Exception)`-around-`slog.warning` block (owned expressions like
`observed_guided.step.value`, `current_turn["type"]`,
`scrub_text_for_audit(…)` evaluated inside the suppression). Fixed
structurally: all four such sites in guided.py (contract-rejection
warning, `_preflight_or_sanitize`, RESPOND terminal-failure, RESPOND
settlement-record-failed) now assemble fields eagerly in the caller frame
and emit through `_log_last_resort_diagnostic`, whose single
suppress-guarded call is the only surviving R7 (see § New sites). The
key's own site (post_guided_start's cancellation-arm
`except Exception as failure_exc: raise exc from failure_exc`) preserves
the primary cancellation while chaining the settlement failure — restage.

Proposed rationale for the start arm: the broad catch does not discard
the caught exception — it re-raises the primary cancellation WITH the
settlement failure chained as `__cause__` (`raise exc from
failure_exc`), so both propagate to the ASGI error machinery; R4's
lexical no-re-raise heuristic cannot see the chained raise.

### `R5:_schema8_permitted_plugins:fp=b3bab096b7423d81` — fixed (086f442c8)
Exact-dict option reads (`type(option) is not dict`), matching both Turn
producers' recursive `dict(deep_thaw(...))` contract; the
Mapping-tolerant form was latent recovery from a hypothetical producer
bug. Offensive `InvariantError` raises unchanged.

### `R5:_schema8_require_runnable_sink_form:fp=501cc36e61268514` — fixed (086f442c8)
Unlike the prefill sites this key's rationale text describes, the actual
flagged value is `body.edited_values` — CLIENT-authored Tier-3 request
content — so the structural form applies:
`@trust_boundary(tier=3, source_param="body", suppresses=("R5",),
non_raising=True)`. The boundary's declared contract on a malformed shape
is return-not-raise (closed-shape violations are the schema transition
contract's to reject); only well-shaped submissions are forwarded to the
deployment sink admission gate, whose own typed
`SinkAdmissionRejectedError` is a policy verdict, not malformed-input
handling. The function body contains no raise, satisfying the
non-raising gate mechanically.

### `R5:_schema8_schema_authority:fp=2d12d5d6dd55504a` and `fp=89853b7545d0d3b0` — fixed (086f442c8)
Same producer contract, same fix: `type(knobs) is not dict or
type(prefilled) is not dict` replaces the two Mapping checks on one line;
`InvariantError` crash arm unchanged.

### `R6:post_guided_start:fp=88dc1a9037d3c07f` — fixed+restage (086f442c8)
`except GuidedOperationFenceLostError: continue` now rejoins inside a
bounded loop with a terminal `AuditIntegrityError` (the judged
missing-fall-through defect). Residual fence-loss `continue` restages.

Proposed rationale: fence loss is an explicit concurrency signal, not a
swallowed failure — the `continue` re-enters
`reserve_or_replay_guided_operation`, which joins the winner
(hash-verified replay via `_replay_completed`,
routes/guided_operations.py:192-211) or performs the sole takeover, and
the loop is bounded by `_GUIDED_FENCE_REJOIN_ATTEMPTS` with a terminal
`AuditIntegrityError` raise when rejoins exhaust.

### `R7:post_guided_respond:_preflight_attempt:fp=cb23bb26ad1c2904` — fixed (086f442c8)
The contract-rejection `suppress(Exception)` block in
`_preflight_attempt` is gone: rejection fields (including the
`WebSurfacePolicyRejectedError` branch and the scrubbed message) are
assembled un-suppressed, and only the emission is guarded inside
`_log_last_resort_diagnostic`. The unconditional 400
`raise HTTPException` follows unchanged. The `WebSurfacePolicyRejectedError`
`isinstance` now sits outside any suppression as plain exception-hierarchy
dispatch (new R5 site — see § New sites).

## web/sessions/routes/composer/guided_chat_atomic.py

### `R4:post_guided_chat_schema8:fp=25dda513de73b5a5` — restage
Site: `except GuidedOperationFenceLostError: rejoin_after_lock = True` at
the settlement loop (validated by the judge as consistent; blocked only
because the pinning test was outside the judge's read roots). The test
exists and is verifiable:
`tests/integration/web/composer/guided/test_chat_schema8_atomic.py::test_expired_operation_takeover_fences_stale_worker_and_both_join_winner`
(defined at line 1153).

Proposed rationale: fence loss sets the rejoin flag; after the compose
lock the handler calls `reserve_or_replay_guided_operation`
(hash-verified replay through `_replay_completed`,
routes/guided_operations.py:192-211) and raises
`AuditIntegrityError("Guided Chat fence was lost without a joinable
winner")` when no winner is joinable — recorded signal, fail-closed
tail. Pinned by
`tests/integration/web/composer/guided/test_chat_schema8_atomic.py::test_expired_operation_takeover_fences_stale_worker_and_both_join_winner`.

### `R5:_step_1_uploaded_bind_form_options:fp=5e9f641fc3d280be` — fixed (9532622d9)
The site's own comment conceded the Mapping-tolerant read was "a latent
robustness argument, not a live population". Exact-dict read
(`type(prefilled) is not dict`) with the `AuditIntegrityError` crash arm
retained; the dead post-thaw re-check deleted.

### `R5:post_guided_chat_schema8:fp=069de33d36caf624` — restage
Site: the failure-classification `isinstance(exc, (AuditIntegrityError,
InvariantError))` chain in the generic Exception arm — nominal
exception-HIERARCHY dispatch over owned classes (the site's KEEP comment
documents the live subclasses: `GuidedCandidateBindingRejected`,
`QuarantineCleanupError`, `LandscapeRecordError`). Not
decorator-eligible (subject is a caught exception).

Proposed rationale: `isinstance` here is genuine union dispatch over
ELSPETH-owned exception classes for the closed failure-code mapping;
subclass membership is the intended semantics (an exact-type check would
silently reclassify live `AuditIntegrityError` subclasses as
`operation_failed` and lose the integrity signal in the durable failure
record). ADR-032 permits nominal `isinstance` against owned classes for
union dispatch.

### `R6:post_guided_chat_schema8:fp=328e26eef8d2339e` — fixed+restage (9532622d9)
The judged defect (silent `suppress(Exception)` around the cancelled-phase
`_publish_progress`, leaving an active-phase registry snapshot visible) is
fixed: the publication failure is recorded
(`guided.cancelled_progress_publish_failed`, with frames) while the
cancellation stays the declared outcome. Residual `except Exception as
progress_exc:` (record-and-continue) restages as the accepted
record-and-surface form for a first-party progress sink on the
cancellation unwind.

### `R6:post_guided_chat_schema8:fp=4f2d8fcd3d06dd76` — restage
Assigned site: `except (PluginConfigError, InvariantError, TypeError,
ValueError)` around `_prepare_step_1_uploaded_source_bind` (and its twin
around `_schema8_answer_and_project_next`) — the transition-rejection
degrade that produces an explicit `SYNTHETIC_UNAVAILABLE` StepChatResult
with `error_class="StepTransitionRejected"` and restores the
authoritative turn. Judged "potentially satisfies the prescribed form",
blocked only on the unreadable test; the test exists:
`tests/integration/web/composer/guided/test_step_chat_uploaded_source_bind.py::test_incompatible_upload_reports_a_type_mismatch_without_binding`
(line 216).

Proposed rationale: the enumerated contract-rejection classes from the
transition are converted into the surface's declared failure result — a
persisted `SYNTHETIC_UNAVAILABLE` chat turn carrying an error class,
with the authoritative turn unchanged (no partial bind; transition
implementations return replaced sessions). Recorded and surfaced, not
swallowed. Pinned by the nodeid above.

### `R6:post_guided_chat_schema8:fp=555acbd86969221c` — restage
Assigned site: the inner `except GuidedOperationFenceLostError` /
`except guided_route.SinkAdmissionRejectedError as admission_exc` family —
the admission rejection settles as an explicit `SYNTHETIC_UNAVAILABLE`
result with `error_class="SinkAdmissionRejected"` and the wizard stays
authoritative (only the typed admission rejection settles here; other
HTTPExceptions propagate). Same restage rationale shape as 4f2d, pinned
by `test_chat_schema8_atomic.py` sink-admission tests.

### `R6:post_guided_chat_schema8:fp=8130c6e770236ecc` — fixed (9532622d9)
The judged genuine violation (settlement failures suppressed during the
cancellation unwind) is fixed at this file's site: the cancellation arm's
`suppress(Exception)` around `fail_guided_operation_with_audit` is
replaced by typed handling — fence loss keeps unwinding as cancelled
(another worker owns the durable outcome); ANY other settlement failure
raises `AuditIntegrityError("Guided Chat could not record its
cancellation settlement")` chained to the cause.

### `R7:post_guided_chat_schema8:fp=3a4165de9179c99f` / `fp=6480b4ca7ef64b4f` / `fp=7218129add51cc6a` — fixed (9532622d9)
The three R7 suppress sites in this handler are gone: (1) cancellation
settlement → typed handling (above); (2) cancelled-progress publication →
recorded; (3) terminal-failure diagnostic → `_log_last_resort_diagnostic`
with eager field assembly. (The parallel guided_plan defects these keys'
rationale texts describe are fixed under 69765dcc5.)

## web/sessions/routes/composer/guided_chat_intent_management.py

### `R6:_has_unmentioned_unavailable_action_identity:fp=5766d1e3edb3d42d` — restage
`except ValueError: return False` around `PluginId.parse` of the
MODEL-authored `catalog_kind:catalog_name` is a recognizer verdict: an
identity that defeats the grammar is "not the unavailable-catalog seam".
Nothing is accepted on that verdict — the caller proceeds to
`validate_deferred_intent_action` (guided_chat_intent_management.py, the
`apply` path directly below the recognizer call; deferred_intents.py:1770)
on the covered path. New pinning test added (b28e70b65):
`tests/unit/web/sessions/routes/composer/test_guided_chat_intent_management.py::test_unparseable_catalog_identity_is_not_the_unavailable_seam`.

Proposed rationale: Tier-3 recognizer over model-authored catalog
identity; `ValueError` from `PluginId.parse` is the typed
grammar-rejection signal and `False` is the predicate's declared verdict
("do not synthesize the unavailable-catalog teaching turn"). Every action
that receives the False verdict still flows through
`validate_deferred_intent_action` at the single call site, so a
malformed identity is never silently retained as verified intent. Pinned
by the nodeid above.

## web/sessions/routes/composer/guided_plan.py

Site map for the six `R4:post_guided_plan` keys (`1c78a9c3…`,
`3cdbef13…`, `9c71acbf…`, `b25ab455…`, `ef24d529…`, `ff8e74c2…`): the six
R4 sites in this route are the fence-lost winner lookup (main arm), the
completed-cancellation replay, the cancellation-arm winner lookup, the
cancellation-arm settlement failure, the generic-arm winner lookup, and
the generic-arm settlement failure. Dispositions by site:

### Generic-arm laundering (`fp=1c78a9c32051443e`, validated GENUINE) — fixed (69765dcc5)
`_guided_full_failure_code` now classifies `ComposerPluginCrashError`
BEFORE its `ComposerServiceError` superclass (the isinstance-chain mirror
of the CCO1 except-ordering gate), returning `operation_failed` — a
plugin crash is never labeled `provider_unavailable`.

### Cancellation-arm settlement failure (site `except Exception as cleanup_exc`, cancel arm) — fixed (69765dcc5)
After the bounded secondary note, the handler now raises
`AuditIntegrityError("Guided PLAN could not record its terminal
failure")` chained to the settlement error, instead of preserving the
cancellation response over an unsettled operation row. Integration test
re-pinned: `test_guided_full.py::test_guided_full_cancellation_settlement_error_surfaces_integrity_error`.

### Generic-arm settlement failure (site `except Exception as cleanup_exc`, generic arm) — fixed (69765dcc5)
Mirror fix; re-pinned by
`test_guided_full.py::test_guided_full_failure_settlement_error_surfaces_integrity_error`.

### Generic-arm winner lookup (site `except Exception as lookup_exc`, generic arm; judged GENUINE) — fixed (69765dcc5)
A bug in the system-owned `reserve_or_replay_guided_operation` now
propagates (`raise` after the note) instead of being replaced by a coded
failure derived from the earlier primary.

### Main-arm winner lookup (`fp=14e746ff…`-adjacent site, `except Exception as lookup_exc` at the fence-loss arm) — restage
Note + terminal progress + bare `raise` of the primary fence loss; every
reachable branch raises or returns the joined winner.

Proposed rationale: preserve-primary form — the secondary lookup failure
is recorded via `_note_guided_full_secondary_failure` (fields assembled
un-suppressed; last-resort emission), and the bare `raise` directly
below re-raises the primary `GuidedOperationFenceLostError`, so no
failure is discarded. Pinned by
`tests/integration/web/composer/guided/test_guided_full.py::test_guided_full_no_winner_after_failure_fence_loss_preserves_primary_outcome`.

### Completed-cancellation replay + cancellation-arm winner lookup (two sites) — restage
Both record the secondary through the note helper and preserve the
primary: the completed-settlement arm re-raises the cancellation
unconditionally after publishing terminal progress; the cancellation-arm
lookup falls to the shared tail, whose every branch raises or returns the
joined winner (`raise exc from settlement_failure` / 499 / bare `raise` /
`raise_guided_operation_failure`, typed `-> Never`).

Proposed rationale (each): the operation's durable outcome is already
established (completed settlement, or another worker's takeover); the
broad catch records the secondary failure through
`_note_guided_full_secondary_failure` and preserves the primary outcome
through the shared tail, in which every reachable branch raises or
returns the durable winner — no path returns a fabricated success.

### `R4:_publish_guided_full_terminal_preserving_primary:fp=14e746ff3fc66d7d` — fixed+restage (69765dcc5)
The judged defect (cannot distinguish sink-originated `CancelledError`
from cancellation injected into the task at the await) is fixed:
`Task.cancelling() > 0` re-raises injected cancellations; only
sink-internal cancellation and ordinary exceptions are recorded as
secondary. Residual `except Exception` (record-and-return) restages as
the accepted secondary-sink form.

### `R5:_guided_full_failure_code:fp=e88ba2e64a35ea45` — restage
The `isinstance` chain is nominal exception-hierarchy dispatch over owned
classes into the closed failure-code vocabulary (same form as the six
previously signed entries on this function, which my
`ComposerPluginCrashError` arm has staled — hub re-stages all seven arms,
see § Shared-baseline).

### `R6:post_guided_plan:fp=a4355ed01d13312f` — restage
Actual guided_plan R6 site: `except GuidedOperationFenceLostError as
fence_lost` in the cancellation arm — fence loss during the failure write
routes to the winner lookup, and the shared tail raises or returns on
every branch (the judge's own reading of 3cdbef13: "R6 appears to misread
shared-tail control flow as a silent continuation"). Restage with the
shared-tail rationale above. (The schema-probe defect this key's
rationale text describes is fixed under 83b4f56e4 — see schema.py.)

### `R7:_note_guided_full_secondary_failure:fp=2571b0089c4eea64` — fixed (69765dcc5)
The helper's own suppress no longer covers field assembly
(`_safe_frame_strings`, `_failure_log_request_id` run un-suppressed); it
delegates emission to `_log_last_resort_diagnostic`. (The service.py
`sources` parse this key's rationale text describes is adjudicated under
the service.py section: restage of the create-event boundary checks.)

## web/sessions/routes/composer/state.py

### `R6:_surface_imported_interpretation_review_events:fp=6f290ff95a440937` — restage
Site: `except ValueError: continue` around
`service.create_pending_interpretation_event` in the YAML-import advisory
surfacer (the W1 backstop). The skipped item is an ADVISORY surface event
for a state that is already durably persisted; the enforcement control is
not this surfacer but the run-time gate:
`materialize_state_for_execution` → `interpretation_sites`
(`web/interpretation_state.py`), which refuses execution while a pending
requirement is unresolved regardless of whether a review card was
surfaced. The per-kind writer boundary (`ValueError` arms of
`create_pending_interpretation_event`) is the necessary-but-not-sufficient
duplicate the comment documents.

Proposed rationale: the caught `ValueError` is the interpretation-event
writer's own typed rejection; raising at this post-persist seam would
500 an import whose state already saved. The skipped advisory surface
stays fail-closed at the execution gate — named control:
`web/interpretation_state.py::materialize_state_for_execution` via
`interpretation_sites`, which raises for unresolved pending
requirements — so no user-visible decision is lost, only a convenience
card. Deliberately not logged: a skipped advisory surface is neither an
audit nor a telemetry event under the primacy policy.

## web/sessions/routes/sessions.py

### `R4:register_session_routes:fork_from_message:fp=673fc0b174a5249e` — fixed+restage (83b4f56e4)
Site: the fork `except Exception as primary_exc:` compensating arm. Fixed
part: blob-cleanup failures are now RECORDED
(`session.fork_blob_cleanup_failed`, per-blob for `cleanup.errors`) — the
judge's e88ba-text finding that `add_note` on a never-re-raised primary
reaches nobody was correct (the tail raises a fresh `HTTPException`
through `raise_guided_operation_failure`, which FastAPI answers without
logging the chain). Residual broad catch restages.

Proposed rationale: compensating broad catch that terminates on every
path — failure classified into the closed vocabulary, durably recorded
via `service.fail_guided_operation` (fenced CAS; raises
`GuidedOperationFenceLostError` when it loses, service.py:5020-5047, in
which case only the fail-CAS winner owns cleanup and the loop rejoins),
blob compensation attempted with per-failure notes AND last-resort log
records, then `raise_guided_operation_failure(failed)` (typed
`-> Never`). Pinned by
`tests/unit/web/sessions/test_fork.py::TestForkEndpoint::test_partial_copy_stale_worker_takeover_completes_without_stale_cleanup`
(exists at test_fork.py:1997).

### `R6:…fork_from_message:fp=99d44e824998a873` — fixed+restage (83b4f56e4)
Outer fence-loss `continue` (`except (GuidedOperationFenceLostError,
BlobForkFenceLostError)`): now inside a bounded loop
(`_FORK_FENCE_REJOIN_ATTEMPTS = 5`) with a terminal
`AuditIntegrityError`. Residual restages with the bounded-rejoin
rationale (mirror of guided START).

### `R6:…fork_from_message:fp=b6fce92fc911d87d` — restage
Inner `except GuidedOperationFenceLostError: continue` on the
fail-CAS: losing the failure CAS means another worker owns settlement
AND cleanup ("Only the fail-CAS winner owns cleanup"); the rejoin joins
the winner's durable outcome, inside the same bounded loop. (This key's
rationale text — `creation_event.payload` Tier-1 re-check — is fixed in
service.py under b28e70b65.)

### `R6:…fork_from_message:fp=ed6c4ebdfb0874e7` — fixed+restage (83b4f56e4)
The narrow cleanup catch (`AuditIntegrityError | BlobError |
SQLAlchemyError | OSError`) now records each failure on the last-resort
channel in addition to the traceback notes; the primary failure is
surfaced through the typed terminal raise. Residual restages: enumerated
compensation-failure classes, recorded, primary preserved; the failed
child is retained as archived audit evidence (deleting it would destroy
the frozen plan envelope). (This key's rationale text describes the
structural `_matching_pending_requirement_index` boundary — done in
service.py under b28e70b65.)

## web/sessions/schema.py

### `R6:probe_current_schema:fp=91a51566a529eeec` — fixed+restage (83b4f56e4)
The judged defect (a435-text): `_validate_current_schema` runs the
model-layer `_validate_partial_index_dialect_symmetry` first, so a
first-party Index declaration bug was converted into `False` → recorded
as `SchemaState.STALE` → operator told to delete a healthy DB. Fixed: the
symmetry check now runs OUTSIDE the probe's verdict handler and crashes
on declaration bugs. Residual `except SessionSchemaError: return False`
restages as the probe's genuine DB-state verdict.

Proposed rationale: read-only schema probe whose declared contract is a
boolean "does this database carry the current schema". The
`SessionSchemaError` arms reachable inside the try are exclusively
DATABASE-state verdicts (missing sentinels, table/column/index drift):
the model-declaration symmetry check runs un-guarded above the try and
crashes before the probe can misread a code bug as staleness.

## web/sessions/service.py

### `R5:SessionServiceImpl:_prepare_or_create_pending_interpretation_event:_sync` ×4 (`fp=432abfd0…`, `fp=6df09b0e…`, `fp=75cb3236…`, `fp=ada13206…`) — fixed (b28e70b65)
These four keys bind the shape guards of
`_patch_structured_interpretation_prompt` (requirements list, requirement
rows, `prompt_template_parts` list, part rows — all raising
`InterpretationPlaceholderConsumedError`). Exactly the structural form
the validated rationales prescribe: `@trust_boundary(tier=3,
source_param="options", suppresses=("R5",))` with pinning test
`tests/unit/web/sessions/test_interpretation_trust_boundaries.py::test_patch_structured_interpretation_prompt_rejects_non_list_requirements`
and recorded AST fingerprint. All four sites now surface as
`R_TB_SUPPRESSED`.

### `R5:_matching_pending_requirement_index:fp=1bb8f339dba8606c` — fixed (b28e70b65)
`@trust_boundary(tier=3, source_param="requirements_value",
suppresses=("R5",))`, raising `InterpretationPlaceholderConsumedError`;
pinning tests `…::test_matching_pending_requirement_index_rejects_non_list`
and `…_rejects_non_mapping_entry`.

### `R5:_require_mapping:fp=3fecf5ceffabd1b3` — fixed (b28e70b65)
`@trust_boundary(tier=3, source_param="value", suppresses=("R5",))`,
raising; pinning test `…::test_require_mapping_rejects_non_mapping`. (The
`Sequence`-re-check complaint in this key's rationale text concerns the
`_value_references_parent_blob` walker — see below.)

### `R5:_patch_structured_interpretation_prompt` ×4 (`fp=37fad632…`, `fp=b5a9aa0f…`, `fp=bbac4cd2…`, `fp=c1250f0f…`) — fixed (b28e70b65)
These keys bind the `_reviewed_content_identity` scans. Two fixes:
(1) the judged GENUINE defect (37fad-text: non-mapping requirement rows
silently excluded; a malformed value bypassed the structured arm into the
legacy `prompt_template` fallback) is retired by the new strict
discriminator `_has_matching_vague_term_requirement` — a PRESENT
malformed requirements value now raises
`InterpretationPlaceholderConsumedError`; only `None` (absent) reads as
the legacy case; (2) the parts scan reads `part["kind"]` /
`part["requirement_id"]` directly because
`prompt_structure_hash_from_options` → `_prompt_parts` has already parsed
and validated exactly that value (raising on malformed parts), so the
Mapping re-check was defensive revalidation of a just-proven contract.
Behavior pinned by
`…::test_reviewed_content_identity_rejects_malformed_requirements`.

### `R5:_classify_authoritative_composition_proposal:fp=f2bd67f022626ce4` — fixed (b28e70b65)
Two sites under this key's text: (1) `creation_event.payload` is a frozen
first-party `ProposalEventRecord` field (`Mapping[str, Any]`,
`freeze_fields` in `__post_init__`) — the `isinstance` re-check deleted,
key-set dispatch reads it directly; (2) the judged legacy-fallback defect
in `_resolve_vague_term` (`has_structured_site` silently False for
malformed values) is retired by the strict discriminator (raises on
malformed, `require_pending=True`).

### `R5:_resolve_vague_term:fp=bf3a3c52ec807a22` — fixed (b28e70b65)
This key's validated rationale adjudicates
`_run_sync_with_post_commit_projection`: `project(result)` could raise
before the captured cancellation was re-raised, losing it. Fixed —
`project` is now guarded and the captured cancellation is re-raised on
every exit path (`raise cancellation from projection_failure`).
(`_resolve_vague_term`'s own scan is the strict-helper fix above.)

### `R5:_resolve_vague_term:fp=df1eabac88dfce67` — fixed (b28e70b65)
This key's validated rationale adjudicates the `_settlement_fork_blob_plan`
candidate scan: `except (TypeError, json.JSONDecodeError): continue`
swallowed wrong-typed content and let a corrupt row vanish while the
exactly-one-plan gate passed on another. Fixed: `TypeError` no longer
caught (content is non-nullable `str`); an undecodable `session_fork`
audit row raises `AuditIntegrityError`.

### `R5:_reviewed_content_identity` ×3 (`fp=473c1ad9…`, `fp=6909abb6…`, `fp=84eb940f…`) — fixed (b28e70b65)
The three scan sites in `_reviewed_content_identity` (structured-match
any(), its row predicate, the parts any()) are retired by the strict
discriminator + direct part reads, as above.

### `R5:_value_references_parent_blob` ×2 (`fp=42fc7aa9…`, `fp=9cf557c7…`) — fixed (b28e70b65)
`@trust_boundary(tier=3, source_param="value", suppresses=("R5",),
non_raising=True)`: a recursive walker over arbitrary composer-authored
JSON-shaped nesting must discriminate node kinds structurally; the
boolean verdict is enforced by its caller
(`_verify_fork_settlement_blob_custody`, which raises
`AuditIntegrityError` on custody violations). The function contains no
raise, satisfying the non-raising gate mechanically.

### `R6:SessionServiceImpl:_run_sync_with_post_commit_projection:fp=ce388895fc178af4` — fixed+restage (b28e70b65)
Fixed as under bf3a3 above. The residual
`except asyncio.CancelledError as exc:` capture arm restages.

Proposed rationale: the drain loop captures the FIRST delivered
cancellation while shielding the worker to completion, and the captured
`CancelledError` is deterministically re-raised on every exit path:
worker failure (`raise cancellation from failure`), projection failure
(`raise cancellation from projection_failure`), and success
(`raise cancellation`). Nothing is discarded; the capture exists so a
mid-drain cancel cannot detonate between commit and projection.

### `R6:_settlement_fork_blob_plan:fp=69a90e7ab2bd0db4` — fixed (b28e70b65)
As under df1eabac above (same site; the bundle carried both keys).

### `R7:SessionServiceImpl:archive_session:_sync:fp=e358d8cf0fb2db78` — fixed+restage (b28e70b65)
The judged GENUINE `suppress(OSError)` around `quarantine_root.rmdir()`
is replaced by errno-discriminated handling: `ENOTEMPTY`/`ENOENT` are the
probe's documented no-op verdicts (other sessions' staged directories
under the shared root / already reaped); every other `OSError` is
recorded (`archive_session.quarantine_root_rmdir_failed` with errno).
Hub: delete stale entry `e358…`. Residual `except OSError` restages.

Proposed rationale: the archive/delete is committed and the session's
own staged directory purge either succeeded or raised
`QuarantineCleanupError` above; this trailing `rmdir` only tidies the
SHARED quarantine root. `ENOTEMPTY` and `ENOENT` are expected negative
verdicts of an idempotent racy tidy-up (explicit no-op outcomes, not
failures); all other errno values are recorded on the service log with
session id, path, and errno before returning the committed outcome.

---

## New sites introduced by the remediation (need staging, not in the 66)

- `routes/_helpers.py::_log_last_resort_diagnostic` — ONE
  `with contextlib.suppress(Exception): log_call(event, **fields)` (R7).
  This is the single surviving emission guard that replaced five
  scattered suppress blocks. Rationale: logging is the channel of last
  resort — a logging-stack failure has no lower channel to surface
  through and must never displace the primary outcome the caller is
  surfacing; every field is evaluated in the caller frame before the
  call, so first-party bugs in diagnostic assembly crash honestly.
- `routes/_helpers.py::_watch_disconnect` — narrowed/recorded R4 (see
  a26356 entry above).
- `routes/composer/guided.py` — R5 `isinstance(exc,
  WebSurfacePolicyRejectedError)` now outside the removed suppress block
  (exception-hierarchy dispatch; same rationale family as the failure
  classifiers).
- `routes/composer/guided_plan.py::_guided_full_failure_code` — new
  `isinstance(exc, ComposerPluginCrashError)` arm (R5, exception
  dispatch; must precede the `ComposerServiceError` arm).
- `routes/composer/guided_plan.py::_publish_guided_full_terminal_preserving_primary`
  — split `except asyncio.CancelledError` (conditional re-raise via
  `Task.cancelling()`) + `except Exception` (recorded secondary), both
  R4/R6-family record-and-preserve forms.
- `routes/composer/guided_chat_atomic.py` — cancellation-arm
  `except GuidedOperationFenceLostError: pass` (fence lost during
  cancellation settlement: winner owns the outcome) and
  `except Exception as progress_exc:` (recorded cancelled-progress
  failure); both documented at the sites.
- `service.py::archive_session._sync` — errno-discriminated
  `except OSError` (see e358 entry above).
- `service.py::_has_matching_vague_term_requirement` — new decorated
  boundary (self-suppressing; no per-line entry needed).

## Shared-baseline changes needed

I edited no allowlist/baseline file. The hub needs to apply:

1. **Delete superseded per-line entries in
   `config/cicd/enforce_tier_model/web.yaml`** (site now structurally
   covered by `@trust_boundary` or code removed):
   - `web/sessions/_auto_title.py:R6:maybe_auto_title_session:fp=819a9041915266f8` (stale; replaced by fresh restage)
   - `web/sessions/routes/_helpers.py:R1:_extract_runtime_model_snapshot:fp=dc84631f2ee5050f`
   - `web/sessions/routes/_helpers.py:R1:_extract_runtime_model_snapshot:fp=e29c46933b6a8da5`
   - `web/sessions/routes/_helpers.py:R5:_extract_runtime_model_snapshot:fp=ec9f2703a0710ab5`
   - `web/sessions/routes/_helpers.py:R1:_guided_source_commit_failure_detail:fp=968e9e71e69fd135`
   - `web/sessions/routes/_helpers.py:R5:_guided_source_commit_failure_detail:fp=c2e307ce9f51d64a`
   - `web/sessions/routes/_helpers.py:R5:_guided_source_commit_failure_detail:fp=de0839b7c3868a67`
   - `web/sessions/service.py:R5:_matching_pending_requirement_index:fp=96b0f97fc7203617`
   - `web/sessions/service.py:R5:SessionServiceImpl:_prepare_or_create_pending_interpretation_event:_sync:fp=bf942aa71347c107`
   - `web/sessions/service.py:R5:SessionServiceImpl:_prepare_or_create_pending_interpretation_event:_sync:fp=f6f2e4b5681150af`
   - `web/sessions/service.py:R5:_value_references_parent_blob:fp=dc57b9620e814e6b`
   - `web/sessions/service.py:R7:SessionServiceImpl:archive_session:_sync:fp=e358d8cf0fb2db78`
2. **Drift-repair (sidecar-WINS / re-stage) the remaining signed entries
   my edits staled** — sites unchanged, only ast_path/scope bindings
   shifted. Current full list is the 32 `Stale tier-model allowlist
   entry` lines for `web/sessions/` in a fresh tier_model run at this
   branch head; beyond the deletions above it comprises:
   `guided_chat_atomic.py:post_guided_chat_schema8` (R5 ×2 `24bf…`,
   `2df1…`; R6 ×3 `8139…`, `925f…`, `ecee…`),
   `guided_plan.py:_guided_full_failure_code` (R5 ×6 `0250…`, `05c7…`,
   `686b…`, `aa82…`, `bfc8…`, `e568…` — plus the NEW
   `ComposerPluginCrashError` arm needs a seventh entry),
   `guided.py` (`R5 …_preflight_attempt:1bbf…`, `R6 post_guided_respond
   73bd…`/`8cb7…`, `R6 post_guided_start ad20…`,
   `R7 …_preflight_or_sanitize:1d73…` — NOTE: the two `_preflight*`
   suppress sites were REMOVED by the helper conversion, so those two
   entries delete rather than re-bind),
   `_helpers.py:R7:_cancel_on_client_disconnect:fp=5571…`,
   `schema.py:R5:probe_current_schema:fp=8563…`,
   `service.py` (`R4 _run_sync_with_post_commit_projection 64c4…`,
   `R6 archive_session 4ad8…`).
3. **Stage new/fresh per-line entries** for every restage above plus the
   § New sites list, using the proposed rationale texts.
4. **No masquerade / dynamic-attribute / wire-shape pinned-set changes**:
   a full `--rules all` run at this head shows only tier_model-family
   lines for `web/sessions/` (no new dynamic-attribute or masquerade
   sites; the new helper takes a bound method, not `getattr`).

## Out-of-scope fix forced by the commit gate

`src/elspeth/web/composer/guided/emitters.py` (c3dac430a): the synthetic
`on_write_failure` knob lacked the REQUIRED `KnobField` key `tier`
(elspeth-ca456d9d8d) — a pre-existing mypy `typeddict-item` error that
failed the pre-commit hook for any change importing the module, and a
latent KeyError for consumers reading `field["tier"]` on that knob. One
line (`"tier": "common"`) with a comment. Outside `web/sessions/` — hub
should fold it wherever composer-lane ownership prefers.

## Test evidence

- `tests/unit/web` (scoped command from the brief, `-n 4`,
  `elspeth.__file__` verified in-worktree): **14310 passed, 13 skipped
  (postgres-gated), 0 failed**. A post-commit full re-run surfaced ONE
  failure: `test_emitters.py::…::test_step_2_schema_form_uses_sink_knobs`
  pinned the tier-LESS synthetic knob (the pre-existing defect c3dac430a
  fixes); the pin now asserts the required `tier` key, and the affected
  packages (`tests/unit/web/composer/guided` + `tests/unit/web/sessions`)
  re-ran green (2913 passed, 12 skipped).
- `tests/integration/web/composer/guided`: 814 passed + the 2 re-pinned
  settlement tests pass individually (they previously pinned the judged
  defect; renamed to `…settlement_error_surfaces_integrity_error`).
- `tests/unit/evals/composer_battery/test_battery_capture.py` (cross-checks
  the tool-call outcome projection): 7 passed.
- New pinning tests:
  `tests/unit/web/sessions/routes/test_trust_boundary_helpers.py` (4),
  `tests/unit/web/sessions/test_interpretation_trust_boundaries.py` (8),
  plus the intent-management recognizer test and the re-pinned
  tool-call-outcomes corruption test.
