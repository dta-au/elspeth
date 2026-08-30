# Tier remediation — lane `plugins` (sign-2026-08-30-w1 blocked findings)

Lane ticket: elspeth-af28999b3b (epic elspeth-e561df3c4e). Branch `lane/tier-rem-plugins`.
37 worklist findings + the base.py R3 special case. Every disposition below was
re-derived from the live tree; the worklist's `rationale_attribution` flag was
treated as unreliable (most judge texts were shuffled across findings) and the
paired verdicts were only relied on where marked `drift-paired`.

Conventions used below:

- **fixed** — the flagged pattern no longer exists; the allowlist entry (where
  one exists) goes stale and should be deleted by the hub.
- **restage** — the code (as now committed) is right and a per-line entry is the
  honest form; proposed rationale text is included. Where my fix changed the
  flagged line, the old fp is retired and the hub's `stage_scan` will key the
  successor site; the successor line is named explicitly.
- Scoped test command (green, 7022 passed):
  `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src <main-venv>/python -m pytest tests/unit/plugins -q -n 4`

---

## plugins/infrastructure/base.py

### R3 special case — `BaseTransform.__init_subclass__` hasattr (NOT allowlistable)

**Disposition: fixed (structural).** The descriptor-ness probe
`hasattr(type(declared_schema), "__get__")` is replaced by an explicit MRO
class-dict scan: `any("__get__" in klass.__dict__ for klass in
type(declared_schema).__mro__)` (base.py:597-607). This is *more* faithful than
hasattr: it reproduces exactly the lookup the attribute machinery performs when
deciding descriptor behaviour (type-MRO dicts only; metaclass fallback excluded),
so the guard cannot disagree with the runtime behaviour it protects. The guard
itself (the anti-masquerading control the baseline warns must not be deleted) is
preserved verbatim in behaviour; all demotion tests pass. No suppression, no
allowlist entry, R3 detector no longer fires.

**Baseline consequence (hub):** `config/cicd/masquerade_baseline.yaml` entry
`kind: hasattr, qualname: BaseTransform.__init_subclass__` is now a
stale-baseline-entry (verified by running
`tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`)
— remove it. The sibling `getattr_static` entry still matches and stays.

### R5 ×3 — `_config_named_input_columns` (fp=4d3a3ec0f3b277dd, fp=a6d25337644033e8, fp=fa4bae6d6d03c9d5)

**Disposition: fixed.** The three isinstance sites (old lines 1176/1178/1182)
coerced authored/validated config option values — Tier-3 data (operator YAML or
Web Composer proposals) with no owned type. The shape coercion is hoisted into a
single module-level boundary `_column_names_in_option_value(value)`
(base.py:103-133) decorated `@trust_boundary(tier=3, source_param="value",
suppresses=("R5",), non_raising=True)`; the method now consumes the helper and
carries no isinstance. `trust_boundary.tests` gate green (non-raising mechanical
check passes: the helper contains no raise).

### R5 ×2 — `_reject_fixed_schema_omitting_consumed_fields` (fp=7f013edacfe5adaa, fp=d313cfb326a91d4c)

**Disposition: fixed.** Old lines 743/745 performed the same Tier-3 option-value
coercion inline; both now route through the same `_column_names_in_option_value`
boundary. One boundary retires all five base.py R5 findings.

**Stale signed entries (hub):** `base.py:R5:BaseTransform:__init_subclass__:fp=a7e867a0933c542d`
(the `isinstance(resolved, property)` guard — site unchanged, now at :650, scope
fingerprint drifted from my edits: **re-stage**, rationale still accurate) and
`base.py:R5:BaseTransform:_reject_fixed_schema_omitting_consumed_fields:fp=74f188f87570c3af`
(covered a coercion site that no longer exists: **delete** unless `stage_scan`
finds a live match).

Commit: see `fix(tier-rem/plugins): infrastructure trust boundaries`.

---

## plugins/infrastructure/clients/llm.py

### R1 ×3 — `_extract_usage_from_provider_response` (fp=153b600f7b824428, fp=3ce6385129d45fcc, fp=54f676db05cacf08) — drift_repair

**Disposition: fixed** per the judge's explicit direction (drift-paired verdict on
fp=54f6): the function now carries `@trust_boundary(tier=3,
source_param="usage", suppresses=("R1", "R5"), non_raising=True)`
(llm.py:220-231). The non-raising contract is the honest one — malformed/absent
usage becomes `TokenUsage.unknown()`/explicit `None` counters, never a raise —
and the old allowlist rationale's claim that TBE1 makes the decorator infeasible
is obsolete (`non_raising=True` replaces the raising-test requirement; gate
green). `suppresses` includes R5 so the `isinstance(usage, Mapping)` dispatch is
covered by the same boundary instead of the per-file R5 blanket (the
`plugins/infrastructure/clients/llm.py` blanket's real usage drops by one; its
`max_hits: 3` can drop to 2 whenever the hub next tunes blankets).

**Hub actions:** delete the three per-line R1 entries (now stale). The three
signed R2 entries (fp=0ef9c6bbb5ce37ea, fp=0a4e14a820f0ce1b,
fp=d9296b94f96c253c) cover the `getattr(usage, ..., None)` probes — still
present, now at llm.py:251-253; R2 is not decorator-suppressible, their
rationale text remains accurate, but the decorator shifted line numbers and
scope fingerprint: **re-stage all three as-is**.

**Masquerade baseline (hub):** the `getattr` entry (`qualname:
_extract_usage_from_provider_response, occurrences: 3`) is now reported
"fully-amnestied" (the @trust_boundary decoration amnesties the probes for the
masquerade rule) — remove it (gate-verified, same test as above).

### R4 — `AuditedLLMClient._emit_telemetry_after_audit` (fp=bbfdc3112d58d4a4) — drift_repair

**Disposition: restage** (code additionally hardened). Event construction —
`stable_hash` calls included — is hoisted BEFORE the try (llm.py:339-362), so
the try now wraps ONLY the telemetry callback delivery. Successor site:
`except Exception as tel_err:` at llm.py:375.

Proposed rationale:

> Prescribed best-effort telemetry form, audit-first. Every call path invokes
> `self._record_call(...)` before `_emit_telemetry_after_audit(...)`
> (chat_completion, llm.py:453/468 and siblings), so the Landscape record is
> already durable when this runs. The event is fully constructed before the
> try (llm.py:339-362): the guarded region is exactly one call,
> `self._telemetry_emit(event)`, a bare caller-supplied Callable that admits no
> typed telemetry error. `contract_errors.TIER_1_ERRORS` re-raise
> (llm.py:371-372); programming errors (TypeError, AttributeError, KeyError,
> NameError) re-raise (llm.py:373-374); the residual catch acknowledges the
> delivery failure with `logger.warning("telemetry_emit_failed", ...,
> exc_info=True)`. Invalidated if construction moves back inside the try, the
> re-raise arms narrow, or the audit write stops preceding emission. Same form
> as the accepted `clients/http.py:R4:_emit_telemetry_after_audit` entry.

---

## plugins/infrastructure/telemetry.py

### R4 — `emit_resource_cleanup_failed` (fp=30a6e00b728c2e6a)

**Disposition: restage** (code hardened to answer the judge's actual objection —
the shuffled worklist text for this site is the one citing
`test_cleanup_telemetry_does_not_suppress_unsuppressible_failures`). The
`ResourceCleanupFailed` event is now constructed BEFORE the try and a
programming-error re-raise arm was added (telemetry.py:55-77), so the swallowed
set is provably limited to delivery failures. Successor site: `except Exception
as telemetry_error:` at telemetry.py:74. New pinning tests:
`tests/unit/plugins/infrastructure/test_telemetry.py::test_cleanup_telemetry_reraises_programming_errors`
and `::test_cleanup_telemetry_logs_ordinary_delivery_failure`.

Proposed rationale:

> Best-effort cleanup-health telemetry. The event is constructed before the
> try; the guarded region is exactly `telemetry_emit(event)`, a caller-supplied
> Callable. TIER_1_ERRORS re-raise; programming errors (TypeError,
> AttributeError, KeyError, NameError) re-raise; the residual delivery failure
> is acknowledged via `logger.warning("resource_cleanup_telemetry_failed",
> ...)`. Unsuppressible-failure behaviour pinned by
> tests/unit/plugins/infrastructure/test_telemetry.py::test_cleanup_telemetry_does_not_suppress_unsuppressible_failures
> and the two 2026-08-31 tests above (exact nodeids). Same form as the accepted
> clients/http.py R4 entry.

---

## plugins/infrastructure/templates.py

### R5 — `_DefiniteBindingAnalyzer.visit_FromImport` (fp=29bf0d04ab98f463)

**Disposition: fixed.** The judge's structural conditions were met but neither
decorator contract was established (the old comprehension neither guaranteed
non-raising nor validated). The parse is now strict and raising: only `str`
entries and `(name, alias)` 2-tuples with str alias are admitted; anything else
raises `TemplateError` (templates.py:163-190). Decorated
`@trust_boundary(tier=3, source_param="node", suppresses=("R5",),
test_ref="tests/unit/plugins/infrastructure/test_templates.py::test_from_import_binding_rejects_malformed_names",
test_fingerprint=8d52b5b5…)`. The referenced raising test is new (4 malformed
shapes, parametrized); `trust_boundary.tests` gate green tree-wide.

---

## plugins/llm/model_catalog.py

### R5 ×2 — `read_litellm_model_list` (fp=0dc9935095bf78ba, fp=76057ade1664188a)

**Disposition: fixed.** Both silent-narrowing sites (non-list → `()`, non-str
entries dropped) were genuine violations: version drift on an installed litellm
became indistinguishable from an absent catalog, corrupting validate-time
membership and the persisted digest. The parse is extracted into a raising
boundary `_parse_litellm_model_list(raw)` (model_catalog.py:139-181) decorated
`@trust_boundary(tier=3, source_param="raw", suppresses=("R5",),
test_ref="tests/unit/plugins/llm/test_model_catalog.py::test_parse_litellm_model_list_rejects_malformed_shapes",
test_fingerprint=2561fb2c…)`; a non-list `model_list` or a non-str entry now
raises `TypeError` naming version drift. Behaviour change is deliberate and
judge-directed; the empty tuple remains only for litellm genuinely absent
(`exc.name == "litellm"`), unchanged.

**Stale signed entry (hub):** `model_catalog.py:R8:_module_:fp=4dec8f01fefcd5fb`
(`os.environ.setdefault(...)` at :85 — site unchanged; module scope fingerprint
drifted from my edit): **re-stage as-is**.

---

## plugins/sinks/_audit_export_bundle_effects.py

### R6 ×2 — `cleanup_stale_audit_export_bundle_scratch` (fp=b54f1e5890909ec7, fp=c87ae071eb7f6a6b)

**Disposition: fixed.** Both `except OSError: return 0` handlers (pin at old
:1183, `os.listdir` at old :1188) are deleted; pin/enumeration failure now
propagates (an unreadable bundle parent must not read as a clean sweep), and
`preflight_audit_export_bundle` converts the raise into
`AuditExportBundlePreflightError("stale-scratch cleanup could not pin or
enumerate the bundle parent")` before any probe is created (:1234-1237). Pinned
by `tests/unit/plugins/sinks/test_audit_export_bundle_effects.py::test_stale_sweep_surfaces_unusable_parent`.

**Stale signed entry (hub):**
`cleanup_stale_audit_export_bundle_scratch:fp=49b86e17b683d8ad` — the
`except FileNotFoundError: continue` stat-race arm, unchanged in meaning, now at
:1196; scope fingerprint drifted: **re-stage as-is**.

### R6 — `preflight_audit_export_bundle` probe-cleanup loop (fp=b3922801308255d7)

**Disposition: restage** (code changed from silent to recorded-and-surfaced).
The finally-loop's pin failure is now appended to `probe_cleanup_failures` and,
on the success path, surfaced as
`AuditExportBundlePreflightError("bundle preflight probes succeeded but probe
cleanup failed: ...")` immediately after the try/finally (:1300-1316). Successor
site: `except OSError as cleanup_exc:` at :1292.

Proposed rationale:

> Recorded-and-surfaced, not silent: the handler appends the failed probe name
> and error to `probe_cleanup_failures`, and the first statement after the
> try/finally raises AuditExportBundlePreflightError naming every failure
> (_audit_export_bundle_effects.py:1315-1316), so a preflight whose probes
> succeeded but whose cleanup failed FAILS. When a probe failure is already
> propagating out of the try, that primary error remains the function's failure
> result and the cleanup note yields to it — the parent is demonstrably
> unusable either way. R6 flags the handler because the surfacing raise is
> outside the except block; moving the raise inside a finally would mask the
> primary probe error.

---

## plugins/sinks/_local_file_effects.py

### R6 — `_remove_exact_staging` (fp=86f1b401538d13cd)

**Disposition: fixed.** The `except (LocalFileEffectError, OSError): return`
blanket is deleted. `_snapshot` already returns an `exists=False` snapshot for a
missing path (no exception), a non-matching snapshot returns without touching
the file, `unlink(missing_ok=True)` absorbs the benign race, and every other
failure (permission, I/O, non-regular file in the owned staging namespace)
propagates, chaining over the primary prepare error instead of being discarded
(:384-397) — the judge's verified direction ("does not justify losing a Tier-1
cleanup failure merely to preserve the primary exception").

### R6 ×2 — `cleanup_stale_local_effect_building_files` (fp=0d616e88e0f42891, fp=23cb8d63fbfc3def)

**Disposition: one fixed, one restage** (the two fps cover the lstat and unlink
handlers at old :348/:354; the worklist does not say which is which — the hub's
`stage_scan` re-keys). The unlink handler is deleted outright
(`path.unlink(missing_ok=True)`): **fixed**. The lstat handler is narrowed to
`except FileNotFoundError: continue` (:346-352) — the concurrent-sweep race
only; every other OSError propagates: **restage** the narrowed successor site.
Pinned by `tests/unit/plugins/sinks/test_local_file_sink_effects.py::test_stale_sweep_surfaces_unreadable_building_entries`
and `::test_stale_sweep_skips_entries_removed_by_a_concurrent_sweep`.

Proposed rationale (lstat successor site, :348):

> Concurrent-removal race only: the glob candidate vanished between listing and
> lstat, so the sweep's goal for this entry — the orphan being gone — is
> already met and the skip is the honest outcome. Every other OSError
> (permission, I/O) on a code-owned `..*.elspeth-*.stage.*.building` name
> propagates (pinned by test_stale_sweep_surfaces_unreadable_building_entries),
> so this handler cannot hide an anomaly. Same narrow form as the signed
> FileNotFoundError arm in cleanup_stale_audit_export_bundle_scratch.

---

## plugins/sinks/_remote_object_effects.py

### R6 — `_unlink_owned_stage` (fp=09bf80ac39c80f73)

**Disposition: fixed.** The catch-and-log-warning is deleted;
`path.unlink(missing_ok=True)` covers the one benign outcome and every other
OSError propagates, chaining over a primary effect error where one is in flight
(:37-45; the module's `logging` import went with it). Tests inverted per the new
contract:
`tests/unit/plugins/sinks/test_remote_object_sink_effects.py::test_reaffirmed_prepare_surfaces_owned_stage_cleanup_failure`
and `::test_collision_prepare_surfaces_owned_stage_cleanup_failure` (the old
"survives cleanup failure" pair pinned the violation itself and was rewritten —
in the collision branch the cleanup runs before the typed raise, so the OSError
propagates with no collision context; that ordering is pre-existing).

### R6 ×2 — `cleanup_stale_remote_spool_building_files` (fp=52e00dec3b78f17c, fp=f9d5029adedaa5e3)

**Disposition: one fixed, one restage** — identical treatment to the
`_local_file_effects` sweep: unlink handler deleted (`missing_ok=True`), lstat
handler narrowed to `except FileNotFoundError: continue` (:386-392). Pinned by
`::test_remote_stale_sweep_surfaces_unreadable_building_entries`. Proposed
rationale for the lstat successor site (:390): identical text to the local-file
sweep rationale above with the `.*.body.*.building` glob named.

---

## plugins/sinks/aws_s3_sink.py

### R5 — `_json_value_chars` (fp=572d4c7f5037be92)

**Disposition: fixed.** `isinstance(value, dict)` → `type(value) is dict`
(:437). The judge's contradiction was verified: production rows reach the
estimator rebuilt by `deep_thaw` (contracts/freeze.py converts mapping proxies
and every dict subclass into exact builtin dicts), so a dict subclass here is an
upstream invariant break, now routed to the typed `S3RecordSerializationError`
(recorded diversion) instead of silently widening the Tier-2 contract. The
laundering test the judge called out was rewritten to pin the new contract:
`TestSerialization::test_json_size_estimate_accepts_exactly_what_deep_thaw_emits`
(OrderedDict now rejected alongside MappingProxyType).

**Stale signed entry (hub):** `_json_value_chars:fp=4c89aaaf2894b7d7` — the
`isinstance(value, (list, tuple))` arm, accepted by the judge and untouched, now
at :450; **re-stage as-is** (tuples remain encoder-serializable and deep_thaw
emits both list and tuple sequence shapes).

### R6 — `_serialize_rows_to_spool` (fp=7c1da8075324924c)

**Disposition: restage** (no code change; line shifted to :570 by the comment
block above). The judge's block was evidence-quality only ("TestSerialization"
without a nodeid).

Proposed rationale:

> Deferred typed raise, not a swallow: the handler sets
> `serialization_failed = True` and the very next statement after the handler
> raises `S3RecordSerializationError from None` (aws_s3_sink.py:575-576) — the
> handler exists solely so the encoder's exception, which can quote the
> offending row value, is never chained into the typed failure
> (payload-hygiene). `S3ObjectSizeLimitError` re-raises ahead of it. The typed
> failure is diverted-and-recorded per row by `_preflight_effect_members`
> (aws_s3_sink.py:979-991; signed entries fp=3ae9618688d6846c /
> fp=e455abd50eb7d1bc). Pinned by
> tests/unit/plugins/sinks/test_aws_s3_sink.py::TestSerialization::test_csv_unencodable_value_is_static_failure
> and ::TestSerialization::test_json_size_estimate_accepts_exactly_what_deep_thaw_emits.

---

## plugins/sinks/azure_blob_sink.py

### R6 — `AzureBlobSink._preflight_effect_members` (fp=9e09536f1abe185a)

**Disposition: fixed (narrowed) + restage the successor.** The judge's verified
defect: the raw `(ValueError, TypeError, csv.Error, UnicodeError)` net also
caught this module's own schema/config integrity ValueErrors. The sink now
mirrors its AWS twin: a typed `AzureBlobRecordSerializationError` is raised only
from (a) the encoder wraps inside `_serialize_csv`/`_serialize_json`/
`_serialize_jsonl` and (b) the fixed-schema extra-field preflight — a
row-attributable condition in the one-row probe, exactly the S3
`S3RecordSerializationError` treatment ratified by the existing corpus
(`test_azure_effect_diverts_fixed_schema_extra_and_publishes_good_rows`). The
CUSTOM header-mapping ValueError stays bare and crashes (config-level, raised
outside the wraps). `_preflight_effect_members` catches only the typed error
(:651). Successor site: `except AzureBlobRecordSerializationError as exc:`.

Proposed rationale (successor site):

> Recorded per-row diversion of a typed serialization failure, mirroring the
> signed AWS twin (aws_s3_sink `_preflight_effect_members`, fp=3ae96/e455).
> `AzureBlobRecordSerializationError` is raised only from the encoder wraps and
> the per-row extra-field preflight (azure_blob_sink.py:95-106 documents the
> closed raise set), so this catch cannot absorb the module's config integrity
> ValueErrors — the CUSTOM header-mapping check raises bare ValueError outside
> the wraps and crashes. Every caught failure is diverted via
> `self._divert_row(...)` and sealed into the plan's diverted ordinals and
> diversion attribution. Pinned by
> tests/unit/plugins/sinks/test_remote_object_sink_effects.py::test_azure_effect_diverts_fixed_schema_extra_and_publishes_good_rows.

---

## plugins/sinks/database_sink.py

### R1 — `DatabaseSink.get_post_call_hints` (fp=c946686ebc7efd22) — drift_repair

**Disposition: fixed.** The classmethod now carries `@trust_boundary(tier=3,
source_param="config_snapshot", suppresses=("R1",), non_raising=True)`
(database_sink.py:1105-1119): the snapshot is composer/LLM- or operator-authored
config ELSPETH has not validated, and the `.get("if_exists")` is an advisory
probe whose absence contributes no hint. Hub: delete the stale per-line entry.

---

## plugins/sources/aws_s3_source.py

### R6 — `_download_s3_object` (fp=f10bcbe2e4af8648)

**Disposition: restage, no code change.** The judge verified the deferred raise
but blocked on a materially false rationale (resource-leak claim, unnamed gate).
Corrected rationale:

> Deferred typed raise through a named gate, not a swallow: the handler
> converts the provider read failure into `primary_error = _read_error(...)`
> and breaks (aws_s3_source.py:490-496); the same function later calls
> `_raise_safe(primary_error)` (aws_s3_source.py:529) on every path where
> `primary_error` is set, after `_close_body` has been attempted and its
> outcome recorded as `cleanup_error_type` on the error object
> (aws_s3_source.py:512-527). The deferral exists so the raised
> S3SourceReadError carries the body-close outcome and the content-length
> check verdict — NOT for resource safety: the outer finally
> (aws_s3_source.py:534-538) closes body and spool unconditionally, so an
> immediate raise would leak nothing. Invalidated if `_raise_safe` stops being
> called for a set `primary_error` or the handler stops constructing the error
> object.

---

## plugins/sources/llm/source.py

### R4 — `_LLMSourceLoadSession.__next__` (fp=daa02d0aff8f5f1a)

**Disposition: restage, no code change.** The judge verified every control in
the code and blocked only because the claimed pinning tests were not readable in
its permitted tree. Both exist:
`tests/unit/plugins/sources/llm/test_source.py::test_pre_set_shutdown_remains_primary_when_cleanup_fails`
(line 400) and
`::test_shutdown_does_not_suppress_tier_one_resource_cleanup_failure` (line 451).

Proposed rationale:

> Shutdown-path cleanup containment with Tier-1 primacy: the broad clause wraps
> only `self._rows.close()` on the pre-set-shutdown branch
> (source.py:83-90); `contract_errors.TIER_1_ERRORS` re-raise ahead of it, the
> contained failure is reported through
> `self._source._report_cleanup_failure(resource="load_iterator", error=exc,
> suppressed=True)` (source.py:555, bounded telemetry + structlog), and
> `_release_load_resources` (source.py:580) re-raises Tier-1 failures even
> under suppress_errors. StopIteration remains the branch's declared result —
> cancellation must not be replaced by a cleanup error. Pinned by
> tests/unit/plugins/sources/llm/test_source.py::test_pre_set_shutdown_remains_primary_when_cleanup_fails
> and ::test_shutdown_does_not_suppress_tier_one_resource_cleanup_failure.

---

## plugins/transforms/aws/guardrails_live_check.py

### R7 — `run_guardrail_live_check` (fp=b308d98bbcac8510)

**Disposition: fixed (R7 retired) + restage the successor R4.**
`with suppress(Exception): client.sdk_client.close()` discarded every teardown
failure. The finally now re-raises TIER_1_ERRORS and programming errors and
acknowledges the residual provider-defined close failure with
`logger.warning("guardrail_sdk_client_close_failed", ...)` (:111-127). Successor
site: `except Exception as close_error:` at :121.

Proposed rationale (successor):

> Best-effort teardown of a provider SDK client the function owns, after the
> live-check verdict is already decided: TIER_1_ERRORS and programming errors
> (TypeError, AttributeError, KeyError, NameError) re-raise even during
> teardown; the residual provider-defined close failure is acknowledged via
> structlog with error type and message rather than silently discarded, and
> never replaces the receipt or the GuardrailLiveCheckError already
> propagating. The guarded region is exactly `client.sdk_client.close()`.

---

## plugins/transforms/aws/textract_client.py

### R4 — `_TextractAuditedClient._emit_after_audit` (fp=140c5b59730a1222)

**Disposition: restage** (code fixed to match the claim the judge found false).
The `ExternalCallCompleted` construction — `stable_hash` payload conversion
included — is hoisted BEFORE the try (:395-415); the try now wraps only
`self._telemetry_emit(event)`. TIER_1 and programming-error re-raise arms were
already present and are unchanged. Successor site: `except Exception as error:`
at :420. Proposed rationale: same form as the clients/llm.py R4 text above, with
`_record_call` preceding `_emit_after_audit` on all three call paths
(textract_client.py:559-575, 667-683, 822-838 after the hoist shift) and the
guarded region being exactly the one callback call.

---

## plugins/transforms/blob_fetch.py

### R1 — `_normalized_content_type` (fp=5248eddc9cf3f217) — drift_repair

**Disposition: fixed.** The function now carries `@trust_boundary(tier=3,
source_param="response", suppresses=("R1",), non_raising=True)`
(blob_fetch.py:203-217): external HTTP response headers, absent content-type
becomes `""`, which cannot match any configured allowed content type and
surfaces as the row's `unsupported_content_type` error. Hub: delete the stale
per-line entry. (The two R1 sites at :501/:504 — headers.get in the error-record
construction inside the transform body — are NOT part of this lane's worklist
and are left untouched.)

---

## plugins/transforms/llm/langfuse.py

### R4 ×3 — `record_success` (fp=bd297d9e5e8363c5), `record_error` (fp=f6c1ec4bd94a8d0e), `flush` (fp=efc3be07b90da7b5)

**Disposition: restage ×3** (record_success/record_error additionally fixed).
The judge's verified defect in the two record methods — `audit_messages(messages)`
(first-party projection) executing inside the try — is fixed: the projection is
hoisted to `traced_input = audit_messages(messages)` before the try
(:152-157 / :215-220), so the guarded region is Langfuse SDK calls only.
`flush` already guarded only `self.client.flush()`. Deliberately NOT added:
programming-error re-raise arms — `_handle_trace_failure`'s docstring records
the prior adjudication (elspeth-a1ab69607a) that a TypeError raised inside the
langfuse SDK is indistinguishable from signature drift by class; the judge's
llm.py comparison does not carry across that seam, and the hoist removes the
first-party code the comparison was protecting. Successor sites: :174 / :232 /
:241.

Proposed rationale (record_success; record_error/flush analogous):

> Optional-telemetry containment at the Langfuse SDK seam. All first-party
> work — metadata, update_kwargs, message list, and the audit_messages
> projection — executes BEFORE the try (langfuse.py:152-157), so the guarded
> region is exclusively `self.client.start_as_current_observation(...)` /
> `generation.update(...)` SDK calls. TIER_1_ERRORS re-raise; the residual SDK
> failure is acknowledged through `_handle_trace_failure` (structlog warning
> with exc_info). Class-based programming-error discrimination inside the SDK
> is deliberately not attempted per elspeth-a1ab69607a (SDK signature drift is
> indistinguishable by class). The provider call this trace describes is
> already recorded in the Landscape before tracing runs:
> `AuditedLLMClient.chat_completion` invokes `self._record_call(...)` before
> returning (clients/llm.py:456 and siblings), and the tracer only ever runs
> on an already-audited call result.

---

## plugins/transforms/llm/providers/azure.py

### R6 — `_configure_azure_monitor` (fp=4619209366032b89)

**Disposition: fixed.** The `except ImportError:` env-var fallback is deleted;
absence of `azure-ai-inference` now raises a remediation-bearing ImportError
(:336-351), mirroring the adjacent `configure_azure_monitor is None` arm.
Verified basis: `enable_content_recording` is policy-bearing, the fallback wrote
`AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` for a reader that could not be
named or verified (the judge's block), and `azure-ai-inference` appears nowhere
in pyproject — the "bundled with elspeth[azure]" claim in the tracing extra's
comment is aspirational, not real. The import is also separated from the
`instrument()` call, so an ImportError from instrumentor internals can no longer
be misread as absence. **Operator-visible behaviour change:** azure_ai tracing
now requires `azure-ai-inference` installed; the failure message says exactly
that. The idempotency guard is not latched on this failure, so install-and-retry
works. Tests updated/pinned:
`test_p1_bug_fixes.py::TestEnableContentRecording::test_missing_instrumentor_fails_closed_without_env_var_policy`
(replaces the two env-fallback tests) and the three `TestConfigureAzureMonitor`
idempotency tests now inject a fake instrumentor module.

**Stale signed entry (hub):** `providers/azure.py:R6:_module_:fp=d94f619bf86df74c`
(the module-level `except ImportError: configure_azure_monitor = None` guard,
unchanged, now visible at :287 because my edit drifted the module scope
fingerprint): **re-stage as-is**.

---

## plugins/transforms/rag/config.py

### R6 ×2 — `_get_providers` (fp=3371a100de2c9052, fp=56767cfd4c9ab197) — drift_repair

**Disposition: fixed ×2.** The drift-paired verdict was verified and is
decisive: `retrieval/azure_search.py` imports NO Azure SDK at all (it speaks the
REST API through httpx, a core dependency), so its `except ModuleNotFoundError:
pass` could only ever have swallowed first-party import failures — the
allowlist rationale's "optional azure-search-documents SDK" claim is false. The
azure_search import is now unconditional (:31-40). The chroma guard (chromadb is
genuinely optional, `[rag]` extra) is narrowed to the litellm-precedent form:
`except ModuleNotFoundError as exc:` re-raises unless `exc.name` is `chromadb`
or a `chromadb.` submodule (:59-67) — a broken chromadb install or missing
first-party module now propagates. Hub: delete both stale entries.

---

## Shared-baseline changes needed (hub applies; verified by gate runs)

`config/cicd/masquerade_baseline.yaml`:
1. **Remove** `path: src/elspeth/plugins/infrastructure/base.py, qualname:
   BaseTransform.__init_subclass__, kind: hasattr` — site no longer exists (R3
   fix). The paired `getattr_static` entry stays.
2. **Remove** `path: src/elspeth/plugins/infrastructure/clients/llm.py,
   qualname: _extract_usage_from_provider_response, kind: getattr,
   occurrences: 3` — probes amnestied by the new @trust_boundary decoration.

Both were confirmed as the ONLY masquerade-gate failures on this branch
(`tests/unit/elspeth_lints/test_masquerade_gate.py`: 243 passed, 1 failed with
exactly these two findings).

`config/cicd/enforce_tier_model/plugins.yaml` — stale entries after this branch
(all verified stale by the keyless tier_model run on this worktree):

Delete (site retired by fixes): rag/config `_get_providers` fp=3371, fp=5676;
clients/llm R1 fp=153b, fp=3ce6, fp=54f6; database_sink fp=c946; blob_fetch
fp=5248; base.py fp=74f188 (unless stage_scan matches a live site).

Re-stage as-is (site unchanged, scope/line drift only): clients/llm R2 fp=0ef9,
fp=0a4e, fp=d929 (now :251-253); base.py `__init_subclass__` R5 fp=a7e8 (now
:650); model_catalog R8 `_module_` fp=4dec; providers/azure R6 `_module_`
fp=d94f; _audit_export fp=49b86e (now :1196); aws_s3_sink `_json_value_chars`
R5 fp=4c89 (now :450).

Re-stage with the new rationale text from this file: clients/llm R4 fp=bbfd
(→:375) plus every "Successor site" named above (telemetry :74,
_audit_export :1292, _local_file :348, _remote_object :390, aws_s3_sink :570,
azure_blob_sink :651, guardrails_live_check :121, textract_client :420,
langfuse :174/:232/:241, aws_s3_source :490, sources/llm/source :87).

Per-file R5 blanket note: `plugins/infrastructure/clients/llm.py` blanket
(`rules: [R5], max_hits: 3`) loses one real hit to the usage decorator —
tighten to 2 at the next blanket pass.

## Lane corpus effect (keyless run, R_TB_SUPPRESSED excluded)

Visible findings in `plugins/` on lane files: 34 → 26; of the 26, 12 are
unchanged not-in-scope or other-lane sites (blob_fetch :501/:504,
rag/transform :441/:450 among them) and 14 are the successor/re-stage sites
listed above. All 37 worklist findings + the R3 special case are adjudicated:
**23 fixed, 14 restage** (for the two sweep fp-pairs, one fp of each pair is
the fixed unlink arm and one the restaged lstat arm; stage_scan re-keys).
