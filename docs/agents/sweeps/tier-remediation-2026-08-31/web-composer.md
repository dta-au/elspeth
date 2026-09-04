# Tier remediation — lane web-composer (sign-2026-08-30-w1 blocked findings)

Lane: `src/elspeth/web/composer/` excluding `guided/` — 75 findings
(ticket elspeth-2da9c5de29, epic elspeth-e561df3c4e).
Branch `lane/tier-rem-web-composer`. Batch commits ("fix commit" for a
finding = the commit touching its file):

- 3b59175e5 — pipeline_planner cancellation/settlement control-flow fix + failing-path tests
- 12b67c75c — yaml_importer structural boundaries + membership forms + 8 raising tests
- 0598be8ba — state.py boundaries, label lookup, coalesce branch-type validation + tests
- ffd8aa449 — planner_authoring_aids / pipeline_proposal membership + Tier-1 direct access
- d18988140 — service provider parse boundary, history marker, protocol posture, reviewed_source_authority
- 6cf32b5ec — tools/* boundaries, owned-union dispatch, Tier-1 splat crash + tests
- 7c4343ff2 — redaction/_required_paths_validator/source_demand/tool_batch/pipeline_planner withheld-count + pinning tests

Raw-corpus measurement (trust_tier.tier_model, shape-only, `--root src/elspeth`,
R_TB_SUPPRESSED and config/cicd lines excluded, `guided/` excluded):
**61 before → 42 after.** Every surviving raw finding is either a deliberate
restage recorded here (including protocol.py's ratified fail-safe fallback),
a newly-visible site flagged for the hub under Shared-baseline changes, or
the pre-existing `tool_batch.py` blanket max_hits overflow (5/3) that is not
in this lane's key set.

Disposition counts: **39 fixed / 36 restage / 0 abandoned.**

Conventions used below:

- *fixed (structural)* — an honest `@trust_boundary` / `@observation_boundary`
  now covers the site; the per-line allowlist entry is redundant and the hub
  deletes it. Raising boundaries carry `test_ref` + gate-computed
  `test_fingerprint`; non-raising boundaries carry `non_raising=True`.
- *fixed (code)* — the flagged pattern is gone (direct access, membership
  form, crash-on-corruption); the finding no longer fires.
- *restage* — the code is right under current semantics; proposed rationale
  text for the new per-line entry is included verbatim.

---

## pipeline_planner.py (4)

### web/composer/pipeline_planner.py:R7:_await_custody_settlement:fp=fdb3466be703648d — restage (after fix 3b59175e5)
The judge's counterexample was real: the drain loop's narrow
`suppress(asyncio.CancelledError)` let a non-CancelledError custody failure
escape at the old :3136, skip the observation step, and REPLACE the active
cancellation; reproduced by a failing test before the fix. Fixed in
3b59175e5: the drain loop suppresses `BaseException` so nothing escapes
mid-loop, and the task outcome is observed explicitly —
`raise cancellation from failure` chains a custody failure onto the
re-raised original cancellation. A `suppress(BaseException)` remains inside
the drain loop (now :3147-3149), so R7 still fires and needs a fresh entry.

Proposed rationale: "The suppress binds ONLY the shielded drain await inside
`except asyncio.CancelledError` (pipeline_planner.py `_await_custody_settlement`):
each pass may raise a repeat cancellation of this coroutine or the custody
task's own failure, and both must stay inside the loop until `task.done()`.
No outcome is lost to it — after the loop, `task.exception()` is read
explicitly and a custody failure is chained onto the re-raised original
cancellation (`raise cancellation from failure`), so the active CancelledError
is re-raised on every path. Pinned by
tests/unit/web/composer/test_pipeline_planner.py::test_await_custody_settlement_custody_failure_does_not_replace_cancellation
and ::test_await_custody_settlement_lets_custody_finish_before_reraising_cancel."

### web/composer/pipeline_planner.py:R7:_settle_lifecycle:fp=3bd7e8749c658bf9 — restage (after fix 3b59175e5)
`suppress(BaseException)` around `task.result()` silently discarded
first-party `lifecycle.on_settled` failures. Fixed in 3b59175e5 with the same
shape as `_await_custody_settlement`: explicit `task.exception()` observation,
failure chained onto the preserved cancellation. The drain-loop suppress
remains (now :3171-3173) and needs a fresh entry.

Proposed rationale: "The suppress binds ONLY the shielded drain await inside
`except asyncio.CancelledError` (pipeline_planner.py `_settle_lifecycle`);
settlement is operator/UI bookkeeping that must complete during teardown.
After the loop the task outcome is observed explicitly: a settlement failure
is chained onto the re-raised original cancellation
(`raise cancellation from failure`), never discarded, and on the
not-cancelled path a failure propagates to the caller's
`except BaseException as settlement_error` evidence-attachment handler at the
`_settle_lifecycle` call site. Pinned by
tests/unit/web/composer/test_pipeline_planner.py::test_settle_lifecycle_records_settlement_failure_on_preserved_cancellation
and ::test_settle_lifecycle_failure_propagates_when_not_cancelled."

### web/composer/pipeline_planner.py:R5:_withheld_component_count:fp=40dcf25aea63041c — restage (after partial fix)
Two defects lived on two lines. The genuine one — `type(withheld) is int
else 0` silently converting first-party envelope corruption into "nothing
withheld" — is FIXED (fix commit below): a non-int now raises
`AuditIntegrityError`, pinned by
tests/unit/web/composer/test_pipeline_planner.py::test_withheld_component_count_crashes_on_corrupt_first_party_count.
The flagged `isinstance(data, Mapping)` line (:2312) is correct and needs a
per-line entry.

Proposed rationale: "`ToolResult.data` is a union-typed owned field: frozen
Mapping payloads (MappingProxyType via `freeze_fields`), lists, or None
depending on the tool, so `isinstance(..., Mapping)` is the only dispatch
that admits the frozen mapping shape (the exact-`dict` idiom is permanently
False on MappingProxyType — same trap pinned for `_merge_component_rejections`
in tools/_common.py). `COMPONENTS_WITHHELD_KEY` is written only by
`_merge_component_rejections` (tools/_common.py) with an int; absence is the
first-class 'nothing withheld' state and returns 0, and a present non-int now
raises AuditIntegrityError instead of defaulting. Pinned by
tests/unit/web/composer/test_pipeline_planner.py::test_withheld_component_count_reads_first_party_int."

### web/composer/pipeline_planner.py:R5:_serialize_provider_discovery_result:fp=51441dc226a24b22 — restage
The structural decorator cannot land: `_serialize_provider_discovery_result`
already carries `@observation_boundary(source_param="result")`, whose
invariant explicitly declares the two `provider_current_state`-rooted
isinstance sites outside its scope, and the metadata contract rejects
stacking a second boundary (`TypeError` at import — verified in-tree). The
per-line entry is the honest form for :3101.

Proposed rationale: "`provider_current_state` is the policy-owned disclosure
projection whose `nodes` entries carry web/LLM-authored content (Tier-3
authorship retained through projection). The `isinstance(candidate, Mapping)`
dispatch selects the disclosed node by identity; a non-mapping or unmatched
candidate is never disclosed — the function answers the explicit fail-closed
`surface_projection_unavailable` payload via `fail_closed()`. A second
`@trust_boundary` cannot be stacked on this function (it already carries the
`result`-rooted boundary; one metadata record per function is enforced at
decoration time). Fail-closed behavior pinned by
tests/unit/web/composer/test_pipeline_planner.py::test_redacted_planner_set_pipeline_arguments_read_fails_closed."

## advisor_checkpoint_telemetry.py (3)

### web/composer/advisor_checkpoint_telemetry.py:R7:record_advisor_checkpoint_pass:fp=796186344ee4ea71 — restage
### web/composer/advisor_checkpoint_telemetry.py:R7:record_advisor_checkpoint_pass:fp=f2af0869d333affb — restage
Sites :42 and :53. Each `with suppress(Exception)` bounds exactly ONE
subordinate telemetry emission (the structlog event at :43-51; the OTel
counter add at :54). The owned canonicalization (`stable_hash`) deliberately
runs ABOVE the suppression at :41 so a first-party canonicalization refusal
propagates — the module comment names the same guard ordering as the signed
`telemetry_phase8` exemption. Fields are closed vocabularies plus a hash;
no advisor findings text is logged raw.

Proposed rationale (both, adjusted for the emit each covers): "The suppress
bounds exactly one subordinate telemetry emission in
`record_advisor_checkpoint_pass`; the checkpoint verdict is complete before
this helper runs and is never replaced by it. `stable_hash` runs above the
suppression on purpose: a canonicalization refusal is a first-party
programmer error and propagates. Pinned by
tests/unit/web/composer/test_advisor_checkpoint.py::TestCheckpointTelemetryHelperShape::test_checkpoint_pass_emit_is_best_effort
and ::test_checkpoint_pass_canonicalization_refusal_propagates (added this
lane)."

### web/composer/advisor_checkpoint_telemetry.py:R7:record_advisor_terminal_publication:fp=83742c6f14dfe177 — restage
Site :93 (and the sibling counter add): same shape — one emit per suppress,
backend-derived closed-vocabulary fields only (module docstring:
elspeth-fa18d54eef attribution event).

Proposed rationale: "The suppress bounds exactly one subordinate telemetry
emission in `record_advisor_terminal_publication`, the branch-attribution
event for advisor-cohort terminal publications (elspeth-fa18d54eef). All
fields are backend-derived closed vocabularies plus the session id; the
publication itself has already happened and is never replaced by this
helper. Pinned by
tests/unit/web/composer/test_advisor_terminal_publication.py::TestTelemetryHelperShape::test_emit_is_best_effort."

## boot_probe.py (1)

### web/composer/boot_probe.py:R6:probe_composer_config:fp=17f8a88608915344 — restage (stale entry; site unchanged, entry drifted)
Convert-to-result probe: `except (LiteLLMAPIError, OpenAIProviderError,
TimeoutError, httpx.HTTPError): return False` at :68-69 is the function's
declared transient-failure result. `LiteLLMBadRequestError` is separately
converted to the fatal `ComposerBootConfigError` (config defect), and
everything else propagates. The caller records False
(`composer_boot_probe_transient_failure` warning with failure_class,
web/app.py:688-696) — the failure is recorded AND surfaced.

Proposed rationale: "Boot-time provider probe with a three-way declared
outcome: True (accepted), raise ComposerBootConfigError on
LiteLLMBadRequestError (operator config rejection — fatal), False on the
typed provider/transport outage classes only (LiteLLMAPIError,
OpenAIProviderError, TimeoutError, httpx.HTTPError). False is the declared
transient-failure result and the caller records it as the
composer_boot_probe_transient_failure warning with failure_class
(web/app.py:688-696); programmer errors propagate. Pinned by
tests/unit/web/composer/test_boot_probe.py::test_probe_is_graceful_on_transient,
::test_probe_is_graceful_on_litellm_provider_error, and
::test_probe_propagates_programmer_errors."

## llm_response_parsing.py (1)

### web/composer/llm_response_parsing.py:R6:_pydantic_extra_fields:fp=ce80cd2a2473b028 — restage
`except AttributeError: return None` at :151-152 covers exactly
`descriptor.__get__(value, type(value))` on a third-party provider object's
`__pydantic_extra__` slot. A declared-but-unset `__slots__` member raising
AttributeError is a legitimate state of a partially constructed third-party
object, not first-party corruption; the boundary answers "no extras". The
descriptor is only invoked when it is a genuine `MemberDescriptorType`
resolved through the MRO, so no provider-controlled code runs (pinned
posture: test_provider_reasoning_does_not_invoke_provider_descriptors).

Proposed rationale: "`value` is a raw third-party provider response object
(Tier-3). The catch covers exactly the slot-descriptor read: a
declared-but-unset `__pydantic_extra__` slot raises AttributeError and is the
legitimate 'no extras recorded' state of a partially constructed provider
object — answered as None, the function's declared absence result. The
descriptor is invoked only when resolved through the class MRO as a genuine
MemberDescriptorType, so no provider-controlled property runs. Pinned by
tests/unit/web/composer/test_compose_loop_llm_audit.py::test_pydantic_extra_unset_slot_reads_as_no_extras_without_raising
(added this lane)."

## pipeline_proposal.py (1)

### web/composer/pipeline_proposal.py:R1:owned_composition_state_review_arguments:fp=8ef410d3a7895726 — fixed (code)
`options.get("mode") == "bind_source"` became membership-then-index, and the
adjacent `options.pop(key, None)` redaction loop became membership-then-del
(the pop was exposed by the same function's signed R9 entry going stale) —
absence of a redactable key in web-authored options is first-class and no
defaulted lookup remains. Finding no longer fires.

## planner_authoring_aids.py (7)

### web/composer/planner_authoring_aids.py:R1:_selected_control_profile:fp=8791a2fa6ae2bd57 — fixed (code)
### web/composer/planner_authoring_aids.py:R1:_selected_control_profile:fp=d94718fb55b15ed3 — fixed (code)
:852-877. All three lookups (`control_modes`, `selected`,
`selected_profile_aliases`) are PARTIAL mappings by contract:
`PluginAvailabilitySnapshot.create` admits any subset (defaults `()`,
web/plugin_policy/models.py:195) and restricted policy views build such
snapshots — a first direct-index attempt was refuted by
TestAutoWireRefusals::test_required_but_unselected_inserts_nothing. Absence
is therefore first-class ("not configured" / "no selection" / "no alias"),
synthesized explicitly after membership; no `.get(default)` remains, so the
findings are retired without masking a broken read behind a default.

### web/composer/planner_authoring_aids.py:R1:discovery_digest:fp=349de0c6d3fffdfd — fixed (code)
### web/composer/planner_authoring_aids.py:R1:discovery_digest:fp=d715ea724a8e7bc5 — fixed (code)
:1776-1785. `knob_schema` is Tier-1 catalog output: every cached schema
passed `validate_knob_schema` at catalog load
(web/catalog/service.py:287), `fields` is a required `KnobSchema` key and
`required` a required `KnobField` key (web/catalog/knob_schema.py:55-84).
Direct access `knob_schema["fields"]` / `field["required"]`; a malformed
schema now crashes instead of publishing an incomplete `required_options`
set.

### web/composer/planner_authoring_aids.py:R1:_build_planner_authoring_aids:fp=bc5ec37eadeb4c7e — fixed (code)
:2713-2733. Same shape as `_selected_control_profile`: `selected` and
`control_modes` are partial by contract, so both defaults (no shield /
RECOMMEND) are synthesized explicitly after membership. Pinned by the
existing TestPromptShieldRules suite (the shieldless-view case exercises the
absent-key path).

### web/composer/planner_authoring_aids.py:R6:build_schema_contract_evidence:fp=0eefb79191188d69 — fixed (code)
### web/composer/planner_authoring_aids.py:R6:build_schema_contract_evidence:fp=82dca06d91637d83 — restage
The `ValueError` catch was removed. Availability is settled against the same
snapshot before `catalog.get_schema` runs, so a failure while reading an
available identity is an internal catalog/profile defect and now propagates.
Pinned by
tests/unit/web/composer/test_schema_contract_carry_forward.py::test_internal_schema_read_failure_propagates.

Proposed rationale (projection_unsupported): "`planner_plugin_contract`
deliberately raises `_SchemaContractProjectionUnsupported` for
non-projectable schemas; the catch converts it into the recorded omission
`{'plugin_id': …, 'reason': 'schema_projection_unsupported'}` published via
`_schema_evidence_envelope`. Recorded-omission form, pinned by
tests/unit/web/composer/test_schema_contract_carry_forward.py::test_unknown_nonprose_schema_keyword_omits_whole_contract_and_reopens_gap
and ::test_malformed_knob_fields_omits_whole_contract."

## protocol.py (1)

### web/composer/protocol.py:R6:ToolArgumentError:code:fp=5a8ebe1f2576739b — restage
A crash-on-missing-slot fix was attempted and REFUTED by the ratified
contract test
tests/unit/web/composer/test_service.py::TestToolArgumentError::test_private_backing_missing_or_wrong_typed_uses_fixed_fallbacks,
which deliberately strips the private slots and asserts every property —
`argument`, `expected`, `actual_type`, AND `code` — degrades to a fixed safe
constant. The design is redaction-safety: these fields render into
LLM/user-facing arg-error payloads, so a tampered or bypass-constructed
instance must yield only fixed safe constants — never attacker-controllable
content and never a crash inside the rendering path. The code now carries a
comment recording this posture.

Proposed rationale: "Fail-safe redaction fallback, not silent recovery:
ToolArgumentError properties are the composer arg-error tamper-degradation
surface. A missing or invalid _safe_code backing slot answers the fixed safe
constant (None), exactly like the sibling argument/expected/actual_type
fallbacks, so a bypass-constructed instance can neither inject content into
the arg-error payload nor crash its rendering. Ratified by
tests/unit/web/composer/test_service.py::TestToolArgumentError::test_private_backing_missing_or_wrong_typed_uses_fixed_fallbacks;
constructed-instance reads pinned by
tests/unit/web/composer/test_audit_arg_error_validation_errors.py::test_tool_argument_error_code_still_reads_constructed_instances."

## provider_telemetry.py (2)

### web/composer/provider_telemetry.py:R4:_log_projection_failure:fp=90ac05f9f48d1bf5 — restage
:67-77. The try body is exactly one `_log.error` emit — the fallback logger
for an already-failed telemetry projection. Both the metric projection and
its fallback log are subordinate to the already-committed audit outcome; a
broken logger must not become a request failure.

Proposed rationale: "The catch bounds exactly one structlog emit — the
fallback operator log for a failed metric projection. Operator metrics and
their fallback logger are subordinate telemetry: the audited request outcome
is already committed by the callers that invoke
finish_composer_request_metrics, and no state mutation sits inside the try.
Swallow behavior exercised (via monkeypatched _log_projection_failure capture)
by tests/unit/web/composer/test_provider_telemetry.py::test_used_token_reset_failure_is_recorded_and_projection_abstains."

### web/composer/provider_telemetry.py:R6:finish_composer_request_metrics:fp=05e41c2c6a05b06a — restage
:202-206. `ContextVar.reset` raising RuntimeError/ValueError (token already
used / from another context) is RECORDED via `_log_projection_failure`
(structured operator log with operation + error type) and the projection
abstains — recorded + declared no-op result, in a telemetry-only module.

Proposed rationale: "A ContextVar.reset failure (already-used or
foreign-context token — RuntimeError/ValueError per contextvars) is recorded
through _log_projection_failure(operation='request', error_type=…) and the
metric projection abstains; the module is operator telemetry subordinate to
the committed audit trail, and no aggregate is fabricated from a broken
token. Pinned by
tests/unit/web/composer/test_provider_telemetry.py::test_used_token_reset_failure_is_recorded_and_projection_abstains
(added this lane)."

## redaction.py (1)

### web/composer/redaction.py:R9:_redact_via_policy:fp=35bb59f664135bcd — fixed (code)
`redacted.pop(key, None)` → `del redacted[key]`: `redacted` is a fresh copy
of `arguments` and `unknown_keys` derives from the same `arguments`, so every
key is present by construction; `del` crashes on a breach of that first-party
membership invariant instead of masking it. Finding no longer fires; the
stale signed entry is deletable.

## required_controls.py (2)

### web/composer/required_controls.py:R6:_parse_candidate_state:fp=9dc9c1a9856c665e — restage
:361-362: the whole-candidate parse returns None on
(KeyError/TypeError/ValueError) — the declared can't-parse result.
Fail-closed chain verified in-tree: `wire_required_controls` returns the
ORIGINAL candidate unchanged when the parse abstains (:793-795), so no
autowiring happens on a malformed candidate, and `validate_plugin_policy`
independently derives required-control findings for the execution gate
(web/plugin_policy/validation.py:209-211).

Proposed rationale: "`_parse_candidate_state` is the declared non-raising
projection of an LLM-authored set_pipeline candidate into an owned
CompositionState for the autowire pass only; None is its documented
can't-parse result. Abstention is fail-closed: `wire_required_controls`
returns the original candidate unchanged (required_controls.py:793-795), so
nothing is authored from a malformed candidate, and required-control
enforcement is independently derived by validate_plugin_policy
(web/plugin_policy/validation.py:209-211) at the execution gate. Pinned by
tests/unit/web/composer/test_required_control_autowire.py::TestAutoWireRefusals::test_malformed_candidate_is_returned_unchanged_without_raising."

### web/composer/required_controls.py:R6:_control_node_is_creditable:fp=d9cf080342b35f97 — restage
:484-487: `_parse_node` failure → False → every caller refuses insertion
(fail-closed against splice churn, per the function's docstring); the
independent authority `node_has_blocking_control` also answers False for
missing/ineffective controls.

Proposed rationale: "`control` is a candidate node block this pass is about
to author; `_parse_node` is the declared raising parse
(required_controls.py:205-217) and a parse failure answers False — 'not
creditable' — which every caller treats as refuse-to-insert, so an
un-authorable control inserts nothing and the diagnosable finding stays
(fail-closed; the fixpoint cannot churn). Pinned by
tests/unit/web/composer/test_required_control_autowire.py::TestAutoWireRefusals::test_uncreditable_control_inserts_nothing_instead_of_churning."

## reviewed_source_authority.py (2)

### web/composer/reviewed_source_authority.py:R5:_authoring_content_hash:fp=8ab5be181c160ed0 — fixed (structural)
`@trust_boundary(tier=3, source_param="options", suppresses=("R5",),
test_ref=tests/unit/web/composer/test_set_pipeline_candidate.py::test_authoring_content_hash_rejects_non_mapping_authoring_metadata,
test_fingerprint recorded)`. Raising boundary: present-but-malformed
source-authoring metadata raises AuditIntegrityError; absence yields None.

### web/composer/reviewed_source_authority.py:R5:resolve_reviewed_source_authority:fp=a21296c2820a4ecc — fixed (structural + code)
Decorator (`source_param="reviewed_facts"`, raising, test_ref + fingerprint
recorded). Also a behavior tightening in the crash-on-corruption class: a
present-but-non-Mapping `reviewed_sources` previously returned None (read as
"no private authority") and now raises AuditIntegrityError; absence still
yields None. Pinned by
tests/unit/web/composer/test_set_pipeline_candidate.py::test_reviewed_source_authority_rejects_non_mapping_reviewed_sources
and ::test_reviewed_source_authority_returns_none_for_absent_reviewed_sources.

## service.py (4)

### web/composer/service.py:R1:_freeform_planner_conversation_context:fp=bdf9b4a54eff5d95 — fixed (code)
:1485-1492. The COMPOSER_HISTORY_USER_AUTHORED_KEY marker is
`NotRequired[Literal[True]]`: absence (assistant entries) skips; any PRESENT
non-True value — including None, which the old `.get()` folded into the
absent path — now raises InvariantError. Pinned by
tests/unit/web/composer/test_service.py::test_freeform_planner_context_present_but_invalid_authorship_marker_crashes.

### web/composer/service.py:R5:_capture_composer_llm_completion_fields:fp=6cb5946996fe0847 — fixed (structural)
### web/composer/service.py:R5:_capture_composer_llm_completion_fields:fp=f4df9bca7f9590d9 — fixed (structural)
`@trust_boundary(tier=3, source_param="response", suppresses=("R5",),
test_ref=tests/unit/web/composer/test_capture_llm_completion_boundary.py::test_malformed_choices_raises_malformed_llm_response_error,
test_fingerprint recorded)` on the declared Tier-3 parse boundary for the raw
provider completion object; malformed shapes raise
_MalformedLLMResponseError. New boundary honesty suite:
tests/unit/web/composer/test_capture_llm_completion_boundary.py (7 tests).
NOTE for the hub: this function also carries four sentinel-`getattr` R2
sites and one R5 site under previously SIGNED entries
(fp=39c79a30/3a647a82/e323244628/f7a9eeae/aeaee190) that the decorator's AST
shift staled — see Shared-baseline changes.

### web/composer/service.py:R4:ComposerServiceImpl:_run_advisor_checkpoint:fp=50c1a511a8289962 — restage (stale entry; code rewritten since it was judged)
The defect the old entry was judged on (a `.get(None)` masking a
code-controlled membership invariant) no longer exists. The current
`except Exception as exc` at :7644 is a bounded-retry convert-to-verdict:
the exception is RETAINED (`last_exc`), retried once, then CLASSIFIED into a
closed failure_class with the fail-closed MALFORMED default (unknown classes
fail closed; only a tight typed/name allowlist maps to UNAVAILABLE), and
returned as the declared AdvisorCheckpointVerdict result. Nothing renders
provider text.

Proposed rationale: "Bounded-retry convert-to-verdict in
_run_advisor_checkpoint: the caught exception is retained as last_exc,
retried once through the same backend arguments contract, then classified
into failure_class with the fail-closed default — only builtin
TimeoutError/ConnectionError and the named typed transport classes map to
'unavailable'; every parse/shape/unknown failure is 'malformed' and blocks.
The verdict is the function's declared result and carries no provider SDK
text. Pinned by
tests/unit/web/composer/test_advisor_checkpoint.py::test_run_advisor_checkpoint_unavailable_after_retries
and ::test_persistently_malformed_response_exhausts_retry_as_malformed."

## source_demand.py (1)

### web/composer/source_demand.py:R6:parse_source_data_contract_accepted_fields:fp=1d8432582742872f — restage (invariant text corrected)
:369-370. The enclosing `@observation_boundary` covers R5 but not R6. The
previous invariant OVERCLAIMED ("callers re-open the card, abstention fails
closed"); corrected this lane to the accurate semantics: abstention strips
nothing from the recomputed demand, so the card can stay closed only when
the independently stored `accepted_artifact_hash` exactly matches the full
current demand — the hash, never this parse, is the acknowledgement
authority (web/interpretation_state.py:1160-1207).

Proposed rationale: "Tier-3 persisted accepted_value text; json parse failure
answers None, the declared abstention. Abstention strips nothing from the
recomputed demand (interpretation_state.py:1169-1176), so a resolved site
stays closed only when its accepted_artifact_hash — the independent hash
binding of the acknowledged field set, compared at
interpretation_state.py:1202-1205 — exactly matches the full current demand;
any demand drift re-opens the card. The hash, not this parse, is the
acknowledgement authority, so no integrity signal is lost."

## state.py (11)

### web/composer/state.py:R1:_composer_node_id_validation_message:fp=2d1bfe60ba92faec — fixed (code)
:222-231. `.get(node_type, "Node name")` → explicit membership.
`_NODE_TYPE_NAME_LABELS` covers every COMPOSER_NODE_TYPES member, so an
unknown node_type here is an unvalidated web-authored value whose rejection
is owned by validate()'s unknown_node_type check in the SAME result set; the
generic label only words this pass's messages and never substitutes for the
rejection (comment records this at the site).

### web/composer/state.py:R1:_well_formed_query_entries:fp=9e7c3748d649a613 — fixed (structural)
The existing `@observation_boundary` on `_well_formed_query_entries` was
widened to `suppresses=("R1","R5")` and its invariant now records the
positional `#<index>` labeling for list entries without a well-formed name
(verified against the code at the site).

### web/composer/state.py:R5:_routing_label_errors:fp=141970439349b78f — fixed (structural) *
### web/composer/state.py:R5:_routing_label_errors:fp=2a978f707cb660a5 — fixed (structural) *
### web/composer/state.py:R5:_routing_label_errors:fp=61cda8329cc3e827 — restage *
### web/composer/state.py:R5:_routing_label_errors:fp=cffa6ca0f84acd15 — restage *
`_routing_label_errors` now carries
`@trust_boundary(tier=3, source_param="nodes", suppresses=("R5",),
non_raising=True)`. Two of the four flagged sites (the `raw_branches`
mapping/list dispatch) are suppressed by it; the two isinstance sites at
state.py:423 (cols 23 and 59: `branch_name`/`connection` type screens inside
the normalized-items loop) still fire raw — the tier_model dataflow walk does
not track the derivation through `dict.fromkeys`/list rebuilding — and need
per-line entries. (*) The fp↔site pairing is opaque to this lane: the hub
should retire whichever two entries no longer fire and restage per-line
entries for the two live state.py:423 sites with the rationale below.

Proposed rationale (both :423 sites): "`branch_name`/`connection` derive from
NodeSpec.branches admitted un-typed from persisted session payloads via
NodeSpec.from_dict (Tier-3). This advisory label rule checks well-typed
labels only and abstains (`continue`) on non-strings; the intrinsic
type rejection is owned by CompositionState.validate — the row_union arm's
row_union_branch_invalid check and the coalesce arm's branch-type check
(added this lane, 'Coalesce … must be a string'), both in the same
validation result set. Pinned by
tests/unit/web/composer/test_state.py::test_coalesce_non_string_branch_connection_is_rejected_at_composition_time
(added this lane)."

### web/composer/state.py:R5:_routing_label_errors:label:fp=6ac935204b62e3f3 — fixed (structural)
The nested `label` closure carries its own
`@trust_boundary(tier=3, source_param="value", suppresses=("R5",),
non_raising=True)`: non-string input yields no label entry; malformed shapes
are owned by the intrinsic node-shape checks.

### web/composer/state.py:R5:_row_union_normalized_branches:fp=8cc745149ad8f449 — fixed (structural)
`@observation_boundary(tier=3, source_param="branches", suppresses=("R5",))`
documenting the normalize-or-preserve-invalid contract (invalid shapes are
preserved for validate() to reject; never raises).

### web/composer/state.py:R6:gate_condition_is_constant:fp=15b9191f52ef1b18 — restage
:2912-2913: diagnosis-only helper answers False for unparseable/forbidden
conditions; the rejection is owned by `_validate_gate_expression` (:2873),
which CompositionState.validate invokes for every gate condition (:7026,
"defense-in-depth catches malformed conditions from any entry path") and
converts both exception types into `gate_condition_invalid` errors.

Proposed rationale: "Diagnosis-only constant-condition probe; syntactically
invalid or forbidden conditions answer False by documented contract. The
integrity rejection is owned by _validate_gate_expression
(state.py:2873-2894), invoked unconditionally for every gate condition by
CompositionState.validate (state.py:7026-7028) and converted to the
gate_condition_invalid error. Pinned by
tests/unit/web/composer/test_state.py::TestStage1Validation::test_gate_malformed_condition_syntax_error."

### web/composer/state.py:R6:_validate_prompt_template_variable_bindings:fp=549623dacbceb3f4 — restage
### web/composer/state.py:R6:_parse_template_names:fp=4a61dc44437dc5f8 — restage
:3456 and :3525: both advisory template rules abstain (return ()/None) when
the template does not parse; the raising rejection is owned by LLMConfig —
`PromptTemplate` failure converts to a pydantic ValidationError at
plugin-config admission (plugins/transforms/llm/base.py
validate_prompt_template), reported as a preflight diagnostic by
validate_runtime_plugins (web/execution/_validation_runtime.py:220).

Proposed rationale (both): "Advisory Jinja2 name-analysis rule; an
unparseable template yields the documented abstention (None / no entries)
because template-syntax rejection is owned by LLMConfig: PromptTemplate
parse failure surfaces as a pydantic ValidationError at plugin-config
admission and as a preflight diagnostic via validate_runtime_plugins. The
ownership seam is pinned by
tests/unit/web/composer/test_state.py::test_template_syntax_rejection_is_owned_by_plugin_config_not_advisory_rules
(added this lane: the same template makes the advisory rules abstain and
LLMConfig raise)."

## tool_batch.py (2)

### web/composer/tool_batch.py:R5:_prevalidation_feedback_seed:fp=1866ee3f2c5a53ef — fixed (structural)
`@trust_boundary(tier=3, source_param="candidate_data", suppresses=("R5",),
non_raising=True)`: frozen ToolResult.data retains external authorship;
Mapping returned as-is, None seeds empty, any other shape carried
structurally under the `candidate_data` key.

### web/composer/tool_batch.py:R5:run_tool_batch:fp=4527c35492ce1855 — restage (stale entry)
The live site (:731) is `isinstance(exc, JsonBoundaryError)` —
exception-type dispatch against an ELSPETH-owned concrete class
(web/composer/bounded_json.py:20), the permitted ADR-032 nominal form; the
matched case converts undecodable LLM argument bytes into the persisted
INVALID_TOOL_ARGUMENTS_REDACTION_STATUS marker.

Proposed rationale: "Union dispatch by isinstance against JsonBoundaryError,
an ELSPETH-owned concrete exception class (bounded_json.py) — the ADR-032
nominal-typing form, not a Tier-3 shape guard. The matched branch replaces
undecodable raw LLM argument bytes with the structured
invalid_tool_arguments redaction marker in both the audit arguments and the
LLM transcript, so the parse failure is recorded, never re-serialized raw."

## tools/_common.py (1)

### web/composer/tools/_common.py:R6:_trusted_requirement_id_for_kind:fp=8984d23c7d49cf5f — restage
:231-232: `parse_interpretation_requirements` failure on EXISTING options
answers None ("no unambiguous trusted id"), so the id-reuse convenience
abstains. Fail-closed: the unconditional stage-B admission gate
`_canonical_interpretation_requirement_error` (_common.py:2368, "it is
unconditional: public writes, trusted proposal replay, reviewed sources, and
internal reconciliation must all pass") re-runs the SAME parse on the
resulting options and converts the same exceptions into the
`interpretation_requirements_invalid` rejection, so malformed requirements
cannot be admitted through this abstention.

Proposed rationale: "Best-effort trusted-id reuse over Tier-3 existing
options; a parse failure answers None and the caller stages a fresh
requirement id. The integrity rejection is owned by the unconditional
stage-B admission gate _canonical_interpretation_requirement_error
(tools/_common.py:2368-2420), which re-runs
parse_interpretation_requirements on the merged options and rejects with
interpretation_requirements_invalid — no malformed requirement can be
admitted through this abstention. Pinned by
tests/unit/web/composer/test_authoring_reconciliation.py::test_unknown_pipeline_decision_user_term_fails_closed."

## tools/_dispatch.py (5)

All five are drift-repair keys over diagnostic FORMATTING helpers for
already-rejected tool arguments; the judge blocked the old entries for a
wrong Tier-3 classification, and separately ruled the structural decorator
inapplicable ("a formatting helper, not the boundary"). The suppressed sites
are unchanged; per-line restage with corrected trust-domain rationales.

### web/composer/tools/_dispatch.py:R5:_schema_error_path:fp=205b88de89bac0c2 — restage
### web/composer/tools/_dispatch.py:R5:_schema_error_path:fp=28f284b409d7c4a5 — restage
Proposed rationale (both sites, :437-443): "`error` is a third-party
jsonschema ValidationError whose `absolute_path` segments mirror the
untrusted LLM-emitted arguments document. The isinstance dispatch renders
each segment into a closed diagnostic vocabulary (identifier, '[]',
'<item>') for the rejection message; validation and rejection already
happened at Draft202012Validator (:512-518), so nothing is admitted,
coerced, or fabricated here. Rendering pinned by the schema-rejection
tests that assert these messages (tests/unit/web/composer/test_tools.py
schema-validation cases)."

### web/composer/tools/_dispatch.py:R5:_json_type_label:fp=7625ff220e5fe782 — restage
### web/composer/tools/_dispatch.py:R5:_json_type_label:fp=814874983450bb01 — restage
Proposed rationale (both, :447-452): "Formats the jsonschema 'type' keyword
value for a rejection message. The only caller passes
`error.validator_value` for validator=='type' (:467), which originates from
the FIRST-PARTY closed root schema built at :510-512 — not Tier-3 data; the
str/Iterable dispatch mirrors the jsonschema draft contract that 'type' is a
string or array of strings. Diagnostic rendering over an owned/third-party
schema contract after rejection; nothing validated or admitted."

### web/composer/tools/_dispatch.py:R5:_schema_error_summary:fp=32635647a6fd3b85 — restage
Proposed rationale (:457): "`error.instance` IS the untrusted LLM-emitted
arguments document; the Mapping check narrows the missing-required-keys
diagnostic branch (compute which required names are absent) in a message
formatter that runs strictly after Draft202012Validator rejected the
arguments. Nothing is admitted or recovered; malformed instances fall
through to the generic summary line."

## tools/blobs.py (4)

### web/composer/tools/blobs.py:R5:_set_nested_option:fp=137c25b3dffd6655 — fixed (structural)
Raising `@trust_boundary(source_param="container",
test_ref=tests/unit/web/composer/test_blob_inline_tools.py::test_set_nested_option_rejects_non_object_segment_collision,
fingerprint recorded)`; new tests added for both raise paths.

### web/composer/tools/blobs.py:R5:_state_options_reference_blob:fp=0a02ea89072aae63 — fixed (structural)
### web/composer/tools/blobs.py:R5:_state_options_reference_blob:fp=b9cfae69a4c2da99 — fixed (structural)
Raising `@trust_boundary(source_param="options",
test_ref=tests/unit/web/composer/test_blob_inline_tools.py::test_state_options_reference_blob_crashes_on_non_str_blob_ref,
fingerprint recorded)`: structural traversal of web/LLM-authored frozen
option trees; a present-but-non-str blob_ref raises AuditIntegrityError
(audited-state corruption) rather than reading as unbound. New tests pin the
crash and a nested-reference hit.

### web/composer/tools/blobs.py:R7:_execute_update_blob:fp=00f584d7c62d2ca2 — restage
:1701 `contextlib.suppress(OSError)` around the post-commit sidecar unlink.
The recovery control is named by symbol and verified:
`reconcile_blob_storage_versions` (web/blobs/service.py:937 — "if the
storage file already matches the committed hash the update committed and the
sidecar is stale (purge it)") runs under the custody lock BEFORE the next
update stages its own sidecar (blobs.py:1424, "Heal crash leftovers (stale
sidecar / delete tombstone) under the custody lock").

Proposed rationale: "Non-fatal post-commit cleanup: the committed bytes
already verify against the committed hash, so a failed sidecar unlink leaves
only a stale journal that reconcile_blob_storage_versions
(web/blobs/service.py:937) deterministically purges under the session
custody lock before any next update stages its own sidecar
(tools/blobs.py:1424). Raising here would turn a fully-committed update into
a spurious error. Pinned by
tests/unit/web/composer/test_tools.py::TestBlobCrashStateReconciliation::test_update_crash_after_commit_purges_stale_sidecar."

## tools/generation.py (6)

### web/composer/tools/generation.py:R5:_attribute_proof_diagnostic_to_source:fp=3e597d35c3bb19bb — fixed (code)
`diagnostic` is detector-owned Tier-1 data (every producer builds
`evidence_locator` as a mapping literal); the defensive isinstance +
RuntimeError re-check was removed — the `{**evidence}` splat crashes with
TypeError on corruption.

### web/composer/tools/generation.py:R5:_row_fields_referenced_by_condition:fp=9ca98b4467ec252c — restage
### web/composer/tools/generation.py:R5:_row_fields_referenced_by_condition:fp=c1a0bda8c660473b — restage
:2140-2165. isinstance against `ast.Subscript`/`ast.Name`/`ast.Constant`/
`ast.Call`/`ast.Attribute` — union dispatch over the Python ast library's
concrete node classes, extracting referenced row-field names for advisory
proof diagnostics. The only caller runs it strictly AFTER
`_validate_gate_expression(condition) is not None → continue` (:2199), so
the condition is already admitted.

Proposed rationale (both): "Union dispatch by isinstance over the Python
`ast` module's concrete node classes while pattern-matching row-field
references in a gate condition for preview proof diagnostics. The condition
is Tier-3 but already admitted: the caller skips any condition
_validate_gate_expression rejects (generation.py:2198-2200), so ast.parse
cannot fail here and the extraction only narrows which field names feed the
advisory diagnostics — no admission, coercion, or recovery."

### web/composer/tools/generation.py:R5:_value_transform_preserves_field:fp=08d1162a997be02f — fixed (structural)
### web/composer/tools/generation.py:R5:_value_transform_preserves_field:fp=dcfcc5591c34166e — fixed (structural)
`@trust_boundary(tier=3, source_param="node", suppresses=("R5",),
non_raising=True)`: malformed operations shapes answer False — the
field-preservation proof abstains (fail-closed unanimity walk).

### web/composer/tools/generation.py:R6:compute_proof_diagnostics:_memoized_resolver:fp=fbcb754da33797c9 — restage
:3333-3336: a UUID parse failure on an external blob_ref is carried
STRUCTURALLY as the collision-separated cache key `("raw", value)` — it can
never alias a canonical UUID entry — and the doomed lookup resolves to None,
for which the preview proof pass abstains by design (:2905 returns no
diagnostics for any unresolved blob). The integrity rejection for malformed
blob_refs is owned by execution admission: `ExecutionServiceImpl._execute_locked`
raises MalformedBlobRefError BEFORE run creation (web/execution/service.py:1568
region), verified together with its pinning test this lane.

Proposed rationale: "The except carries the UUID parse failure structurally
as the collision-separated ('raw', value) cache key — malformed refs cannot
alias canonical entries — and the resolver answers None, the preview proof
pass's documented abstention for any unresolved blob. The rejection of
malformed blob_refs is owned by execution admission:
ExecutionServiceImpl._execute_locked pre-validates the UUID and raises
MalformedBlobRefError before creating the run. Pinned by
tests/unit/web/execution/test_service.py::TestBlobRefPreValidation::test_malformed_blob_ref_raises_before_run_creation."

## tools/sessions.py (1)

### web/composer/tools/sessions.py:R5:_detect_unresolved_interpretation_placeholders_typed:fp=d39651187f78a341 — fixed (structural)
Raising `@trust_boundary(source_param="nodes",
test_ref=tests/unit/web/composer/test_request_interpretation_review_tool.py::test_typed_detector_raises_for_non_string_prompt_template,
fingerprint recorded)`: a present non-string prompt_template raises
ToolArgumentError; existing test pins the raise through `nodes`.

## tools/transforms.py (1)

### web/composer/tools/transforms.py:R5:_execute_upsert_node:fp=b4fa9a3e86a99889 — fixed (code)
`validated.branches` is pydantic-validated as exactly
`list[str] | dict[str, str] | None` (_UpsertNodeArgumentsModel), so the ABC
`isinstance(..., Mapping)` re-check became owned-union dispatch on the
concrete constructed type (`type(validated.branches) is dict`) at BOTH twin
sites (:547 queue path, :705 upsert path — the queue twin's signed entry
ba009579eba8e80c goes stale; see Shared-baseline changes).

## turn_audit.py (1)

### web/composer/turn_audit.py:R6:persist_turn_audit:fp=313271caf351d850 — restage
:110-114: the value being decoded is `tc.function.arguments` — raw
LLM-emitted tool-call argument TEXT (Tier-3, not a round-tripped Tier-1
envelope), on the branch where argument validation already failed
(`error_class is not None`). The parse failure is converted into the
persisted structured marker
`{"_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
"error_class": …}` — recorded and surfaced in the audit row itself; the
non-dict decode is likewise carried structurally (`_decoded_non_object`).

Proposed rationale: "bounded_json_loads over raw Tier-3 LLM tool-call
argument text on the already-rejected-arguments audit path; a
TypeError/ValueError decode failure is converted into the persisted
invalid_tool_arguments redaction marker carrying error_class — the failure
is recorded in the same audit row it describes, never discarded, and raw
undecodable bytes are never persisted as arguments. Marker projection pinned
by tests/unit/web/composer/test_compose_loop_persistence.py::test_current_loop_schema_valid_semantic_arg_error_persists_only_closed_argument_projection."

## tutorial_telemetry.py (1)

### web/composer/tutorial_telemetry.py:R4:record_tutorial_completed_path:fp=b7101414516c103c — restage
:29-35: the try body is exactly the OTel counter add; the closed-vocabulary
validation raises ABOVE the try (offensive guard outside the swallow), and
the caller invokes this only after the preference write committed
(`PreferencesService`: `record_tutorial_completed_path` at
web/preferences/service.py:533-541 runs after `await
run_sync_in_worker(_sync)` returns). No tutorial-special backend path is
involved — this is telemetry about the canary, same backend as every session.

Proposed rationale: "The catch bounds exactly one OTel counter add; the
completion_path closed-vocabulary check raises above the try, and the
preference write that established the outcome has already committed in the
caller (web/preferences/service.py:510-541) before this helper runs, so the
swallow can only lose the optional metric, never the outcome. Pinned by
tests/unit/web/composer/test_tutorial_telemetry.py::test_completed_telemetry_does_not_replace_committed_outcome
and ::test_record_tutorial_completed_rejects_unknown_path."

## _required_paths_validator.py (1)

### web/composer/_required_paths_validator.py:R5:_optional_ancestor_present:fp=b07f5f9d3d7a04bf — fixed (structural)
`@trust_boundary(tier=3, source_param="value", suppresses=("R5",),
non_raising=True)`: walks LLM-emitted tool arguments along a compiled
ancestor path, returns False as soon as a segment is absent or the cursor is
not a mapping; the NotImplementedError guard is conditioned on the
first-party compiled ancestor, not on `value`.

## yaml_importer.py (11)

The file's import surface is now uniformly structural: every keyed helper
carries `@trust_boundary` (raising form, `RuntimeYamlImportError`, with
test_ref + gate-computed fingerprint; `_reject_yaml_aliases` names
`yaml.composer.ComposerError`). All 11 per-line entries are redundant and
deletable.

### web/composer/yaml_importer.py:R5:_reject_yaml_aliases:fp=df83e474a3ab5ea8 — fixed (structural)
### web/composer/yaml_importer.py:R5:_require_mapping:fp=b1142f5012ba0aa9 — fixed (structural)
### web/composer/yaml_importer.py:R5:_require_sequence:fp=8a09d42e28c8efba — fixed (structural)
### web/composer/yaml_importer.py:R5:_require_sequence:fp=c75d851c9859b35d — fixed (structural)
### web/composer/yaml_importer.py:R1:_optional_str:fp=ea969a74d753d545 — fixed (structural)
### web/composer/yaml_importer.py:R5:_optional_str:fp=9d2b47cf4d343858 — fixed (structural)
### web/composer/yaml_importer.py:R5:_route_label:fp=6b63030c8ad380f8 — fixed (structural)
### web/composer/yaml_importer.py:R5:_string_mapping:fp=8ede63bba3df1185 — fixed (structural)
### web/composer/yaml_importer.py:R5:_string_tuple:fp=e55a0bc86c8f17ef — fixed (structural)
Decorators with per-function test_refs added to
tests/unit/web/composer/test_yaml_importer.py (8 new raising tests).

### web/composer/yaml_importer.py:R9:_source_from_runtime_entry:fp=8540a7becba85d3c — fixed (code)
`options.pop("on_validation_failure", None)` → membership-then-pop: absence
of the option is first-class and no defaulted pop remains. (Stale entry
deletable.)

### web/composer/yaml_importer.py:R1:_collector_nodes_from_runtime_lists:fp=80ca16bf974e4f2f — fixed (structural)
`@trust_boundary(source_param="collectors_section", suppresses=("R1","R5"),
raising, test_ref + fingerprint recorded)`. The invariant honestly declares
that an absent options key becomes the empty options dict on the owned
NodeSpec — the owned type's domain semantics at the parse boundary. The
control chain the old rationale leaned on is now fully verified in-tree:
ExecutionServiceImpl._execute_locked pre-validates blob_ref UUIDs before run
creation, and its named pinning test
tests/unit/web/execution/test_service.py::TestBlobRefPreValidation::test_malformed_blob_ref_raises_before_run_creation
exists (the judge could not read the tests tree; this lane can and did).

Additional honest fix in the same file (not a keyed finding):
`_nodes_from_runtime_list` :479-481 — `isinstance(branch_spec, Mapping)` on
the value `_string_mapping` constructed became owned-union dispatch
(`type(branch_spec) is dict`).

---

## Shared-baseline changes needed (hub applies; this lane touched none of them)

1. **Allowlist entry deletions** (config/cicd/enforce_tier_model/web.yaml) —
   every entry keyed above as *fixed*: the site is covered structurally or
   the pattern is gone. This includes the five entries the baseline already
   reported stale (boot_probe 17f8a886 — restaged instead, run_tool_batch
   4527c354 — restaged instead, transforms b4fa9a3e, service R4 50c1a511 —
   restaged instead, yaml R9 8540a7be) — delete the fixed ones, replace the
   restaged ones with the new rationales.
2. **Signed entries staled by this lane's AST churn** (drift, honest per the
   binding-churn rule; the sites are unchanged or improved). The hub should
   re-stage these with their existing (still accurate) rationales or delete
   where the pattern is gone:
   - service.py R2 x4 fp=39c79a30af914831 / 3a647a82bdcabc59 /
     e323244628ffd23b / f7a9eeae1efd19bc and R5 fp=aeaee190329afbd7
     (`_capture_composer_llm_completion_fields` sentinel-getattr parse form —
     prescribed ADR-032; sites unchanged, only shifted by the new decorator).
   - pipeline_planner.py R7 fp=7acdbe01b4ab0451 / 5d7e3fda15cb310d (the OLD
     signed R7 entries for the settlement helpers; superseded by the fix +
     the two restages above).
   - pipeline_proposal.py R9 fp=d7c371a6f5dba00f (pop loop now membership-del
     — pattern gone, delete).
   - planner_authoring_aids.py R1 fp=99775a502e366900 / 914f09ae97e5cfdf
     (patterns gone — delete).
   - reviewed_source_authority.py R5 fp=a4b8df2b4de1d781 / c4f14963037b472e
     (decorator covers — delete).
   - state.py R6 fp=f5e76bbd8e709438 (CompositionState.validate — site
     unchanged, AST shifted by the new coalesce branch-type check; restage
     as-is) and R6 fp=106288f2d48cb983 (_routing_label_errors — the live
     :452 site converts a ValueError into an output_name_invalid
     ValidationEntry via add(); restage as convert-to-result).
   - tools/blobs.py R5 fp=1798166c6e00ea9a / 372ee9b6cc447aa2 /
     8190e912aec5864a and fp=137c25b3dffd6655 (decorators cover — delete).
   - tools/generation.py R5 fp=08d1162a997be02f / dcfcc5591c34166e
     (decorator covers — delete).
   - tools/sessions.py R5 fp=d39651187f78a341 (decorator covers — delete).
   - tools/transforms.py R5 fp=ba009579eba8e80c (queue-path twin — pattern
     gone, delete).
   - yaml_importer.py x10 signed entries (decorators cover / pattern gone —
     delete).
   - _required_paths_validator.py fp=b07f5f9d3d7a04bf, redaction.py
     fp=35bb59f664135bcd (delete).
3. **Newly-visible raw findings NOT in this lane's 75 keys** (exposed when
   the entries above went stale; adjudicated here for the hub's convenience,
   no entries written):
   - state.py:7945 R6 `except PydanticValidationError: continue` —
     abstention with the named owner ("the aggregation's intrinsic trigger
     validator owns malformed external input"); hub restage.
   - state.py:452 R6 — converts ValueError to an output_name_invalid
     ValidationEntry (see item 2).
   - tool_batch.py per-file blanket max_hits 5/3 overflow — pre-existing,
     untouched by this lane.
4. **Masquerade / dynamic-attribute pinned sets**: no new `getattr` sites
   were added in src. New TEST doubles that may register in the masquerade
   inventory (tests included in that gate):
   - tests/unit/web/composer/test_capture_llm_completion_boundary.py
     (SimpleNamespace provider-response stand-ins),
   - tests/unit/web/composer/test_advisor_checkpoint.py
     `_ExplodingLogger` / `_ExplodingCounter`,
   - tests/unit/web/composer/test_pipeline_planner.py settlement-test
     coroutine stubs (plain functions; likely inert),
   - tests/unit/web/composer/test_provider_telemetry.py monkeypatched
     `_log_projection_failure` capture.
   The hub runs the whole-tree gates and applies whatever pinned-set updates
   these require; this lane edited no baselines.
5. The whole-tree dynamic-attribute gate should see no change; the whole-tree
   wire-shape and output-byte gates are unaffected (no wire shapes or output
   bytes touched).

## Files edited (src)

- src/elspeth/web/composer/pipeline_planner.py
- src/elspeth/web/composer/pipeline_proposal.py
- src/elspeth/web/composer/planner_authoring_aids.py
- src/elspeth/web/composer/protocol.py
- src/elspeth/web/composer/redaction.py
- src/elspeth/web/composer/reviewed_source_authority.py
- src/elspeth/web/composer/service.py
- src/elspeth/web/composer/source_demand.py
- src/elspeth/web/composer/state.py
- src/elspeth/web/composer/tool_batch.py
- src/elspeth/web/composer/yaml_importer.py
- src/elspeth/web/composer/_required_paths_validator.py
- src/elspeth/web/composer/tools/blobs.py
- src/elspeth/web/composer/tools/generation.py
- src/elspeth/web/composer/tools/sessions.py
- src/elspeth/web/composer/tools/transforms.py

## Tests edited/added

- tests/unit/web/composer/test_pipeline_planner.py (settlement failing-path
  suite + withheld-count pins)
- tests/unit/web/composer/test_state.py (coalesce branch-type rejection +
  template ownership seam)
- tests/unit/web/composer/test_yaml_importer.py (8 boundary raising tests)
- tests/unit/web/composer/test_capture_llm_completion_boundary.py (new file)
- tests/unit/web/composer/test_set_pipeline_candidate.py (reviewed-source
  authority crash/absence pins)
- tests/unit/web/composer/test_audit_arg_error_validation_errors.py
  (ToolArgumentError constructed-instance code read)
- tests/unit/web/composer/test_service.py (present-but-invalid authorship
  marker crash)
- tests/unit/web/composer/test_advisor_checkpoint.py (telemetry best-effort +
  canonicalization-ordering pins)
- tests/unit/web/composer/test_provider_telemetry.py (reset-failure
  record-and-abstain pin)
- tests/unit/web/composer/test_compose_loop_llm_audit.py (unset
  __pydantic_extra__ slot pin)
- tests/unit/web/composer/test_blob_inline_tools.py (nested-option and
  blob-reference walker pins)

Test command:
`PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src /home/john/elspeth/.venv/bin/python -m pytest tests/unit/web -q -n 4`
— exit status recorded in the lane's final report.
