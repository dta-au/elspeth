# Tier-remediation dispositions — lane core-contracts (elspeth-ca0a7e71b1)

Source: the 77 sign-2026-08-30-w1 blocked findings assigned to this lane
(contracts/ and core/ under src/elspeth). Every judgment below was re-derived
from the tree; the per-finding judge rationales in the lane worklist were
heuristically paired and several were verifiably mis-attributed (noted where it
mattered).

**Counts: 70 fixed · 6 restage · 1 abandoned.**

Commits (branch `lane/tier-rem-core-contracts`):

- C1 `f9fda6b96` — contracts/ fixes and boundaries
- C2 `987cc9692` — core/config.py cluster
- C3 `fbb2c8392` — canonical / expression_parser / secrets / templates / schema_shape
- C4 `e948fb8f6` — landscape repositories / checkpoint recovery

Verification: scoped suite `tests/unit/contracts tests/unit/core -n 4` →
**9291 passed, 1 skipped (pre-existing SQLite-affinity skip)**;
`trust_boundary.tests`/`.scope`/`.tier` gates → **0 findings**;
`scripts/check_contracts.py` → green; targeted mypy over all 22 edited src
files → clean; ruff → clean. Raw tier_model corpus over this lane's files
(empty allowlist dir, R_TB_SUPPRESSED excluded): **451 → 346**; of the 77
blocked keys, **3 remain** in the raw corpus (the two L1 restages and the
barrier_scalars restage below) — every other key's site is retired or now
reports R_TB_SUPPRESSED under an honest `@trust_boundary` contract.

Format note: `fp=` values in keys are the fingerprints as staged in
sign-2026-08-30-w1. Where a site survives with a NEW fingerprint (line/context
drift from these commits), the current fingerprint is given in the entry.

---

## contracts/

### contracts/barrier_scalars.py:R5:BarrierScalars:from_dict:fp=33a3fa0923703db6 — **restage**
Drift repair (stale allowlist binding; code unchanged, fp still live at
line 348). `from_dict` rehydrates the persisted checkpoint column: every shape
check in it exists solely to raise `AuditIntegrityError` on corruption, the
exact crash the tier doctrine prescribes for Tier-1 persisted data
("corruption CRASHES (AuditIntegrityError etc.)"). No defensive default, no
coercion, no suppression.
**Proposed rationale:** "`BarrierScalars.from_dict` (contracts/barrier_scalars.py:306)
rehydrates the persisted checkpoint column value — first-party data that
crossed a persistence boundary and can be corrupt. The flagged check at
line 348 (`isinstance(raw_key, (list, tuple)) … all(isinstance(s, str))`)
is a corruption DETECTOR: its only consequence is
`raise AuditIntegrityError("Corrupted BarrierScalars: coalesce[i] key …")`
(lines 349-353). Every isinstance in this function feeds an unconditional
AuditIntegrityError raise — the prescribed offensive rehydration form; nothing
is defaulted, coerced, or skipped. Pinned by the from_dict corruption suite in
tests/unit/contracts/test_barrier_scalars* (malformed key shape raises)."

### contracts/call_data.py:R5:_require_http_status_code:fp=a22e6d0d1e2b6c43 — **fixed** (C1)
Judge verified: the `assert isinstance(value, int)` re-checked what
`require_int` (contracts/freeze.py:156) had already proven. Removed; the
static narrowing is now `cast("int | None", value)` with a comment citing the
authority — the repo's established pattern (contracts/secret_scrub.py) for
"proven by first-party validator, narrow statically". No runtime re-check
remains.

### contracts/emitted_option.py:R5:env_placeholders_in:fp=9ad67e0daa23143c — **fixed** (C1)
Judge directive followed: structural boundary. `env_placeholders_in` now
carries `@trust_boundary(tier=3, source_param="value", suppresses=("R5",),
non_raising=True)`; unsupported shapes return False. Per-line entry (and any
related ones in the function) should be deleted as dangling.

### contracts/hashing.py:R5:_stable_repr:fp=4d5421fe2ea96b4f — **fixed** (C1)
### contracts/hashing.py:R5:_stable_repr:fp=94f18be4b47a0286 — **fixed** (C1)
One decorator retires both. `_stable_repr` is the deterministic-repr walker on
the `repr_hash` quarantine fallback (docstring: "Appropriate for Tier-3 …
where data is already malformed and being quarantined") — the evidence the
94f1 BLOCK-PENDING verdict said was missing is now declared in the
`@trust_boundary(source_param="obj", non_raising=True)` metadata.

### contracts/identifiers.py:R5:validate_field_names:fp=180737c040065cd8 — **fixed** (C1)
### contracts/identifiers.py:R5:validate_field_names:fp=3f2a952d6f9d6688 — **fixed** (C1)
Drift-repair entries whose fps no longer existed (current tree had
cbc447d7/56182e44 at line 81). Followed the judge's direction:
`@trust_boundary(source_param="names", suppresses=("R5",))` raising contract,
pinned by the new
`tests/unit/core/test_identifiers.py::TestValidateFieldNames::test_non_sequence_names_raises`
(bare str, bytes, and non-sequence each raise ValueError) with the
gate-emitted canonical test fingerprint. Both stale entries are dangling —
delete.

### contracts/results.py:R5:_require_artifact_metadata:fp=ffb91c790069f1d4 — **fixed** (C1)
Judge verified the defect: recursion only entered already-frozen mappings, so
`MappingProxyType({"a": {"b": 1}})` passed unchanged. The recursion now enters
EVERY nested `Mapping`, making a nested plain dict fail the frozen check.
Pinned by two new tests in
tests/unit/contracts/test_results.py (`test_validator_rejects_nested_plain_dict`,
`test_validator_rejects_non_str_nested_key`).
**Surviving derivative site** (same line, new fp `5cd957a737428a01`,
`isinstance(item, Mapping)` at results.py:170) needs a per-line entry.
**Proposed rationale:** "`_require_artifact_metadata` is a construction-time
recursive freeze validator called from `ArtifactDescriptor.__post_init__`
(results.py:642) on a deliberately `object`-typed field. The flagged
`isinstance(item, Mapping)` selects which children to recurse into so that a
nested UNFROZEN mapping reaches the `MappingProxyType` rejection above it and
raises TypeError — the same accepted write-side DTO invariant form as
contracts/plugin_assistance.py `_assert_safe_assistance_value` (ACCEPTED,
sign corpus). Pinned by
tests/unit/contracts/test_results.py::TestArtifactDescriptorDeepFreeze::test_validator_rejects_nested_plain_dict."

### contracts/schema.py:R1:get_raw_producer_guaranteed_fields:fp=19efb91987617016 — **fixed** (C1)
### contracts/schema.py:R1:get_raw_producer_guaranteed_fields:fp=7f33bcb33c95933b — **fixed** (C1)
### contracts/schema.py:R1:get_raw_producer_guaranteed_fields:fp=d573da7c47b411ab — **fixed** (C1)
One decorator retires all three, exactly as the judge prescribed (raising
contract on `options`): `@trust_boundary(source_param="options",
suppresses=("R1",))` with the new nodeid-resolvable test
`…test_schema_config.py::TestSchemaTrustBoundaryCharacterization::test_get_raw_producer_guaranteed_fields_rejects_malformed_schema_block`
and its gate-emitted fingerprint (the missing evidence in the 7f33
BLOCK-PENDING verdict). Related per-line entries → delete.

### contracts/secret_scrub.py:R5:_scrub_value:fp=7cd26d0fe21195e5 — **fixed** (C1)
The judge's open question (raising vs non-raising on malformed input) is
resolved IN CODE: the one path that could raise on malformed input (a non-str
mapping key reaching `_is_secret_key_name`) is now total
(`parent_key=k if isinstance(k, str) else None` — a non-str key cannot name a
secret), making the declared `non_raising=True` contract on
`@trust_boundary(source_param="value")` honest.

### contracts/sink_effects.py:R5:SinkEffectIdentity:__post_init__:fp=5cf02cb7915bc668 — **fixed** (C1)
Judge verified: the isinstance converted a wrong-type Tier-1 `member_ids`
element into the malformed-digest ValueError. The type filter is removed; a
non-str element now crashes with the natural TypeError from
`_LOWER_HEX_64.fullmatch`. Pinned by
`TestSinkEffectIdentityMemberIdTypes` (TypeError for `42`, ValueError for a
malformed string digest).

### contracts/sink_effects.py:R5:_require_bounded_positive_int:fp=c59795b34b8b1c45 — **fixed** (C1)
Same class as call_data a22e: redundant `assert isinstance` after
`require_int` replaced by `cast(int, value)` with the authority cited.

### contracts/sink_effects.py:R5:_verify_signed_manifest_bytes:fp=bc1a10ffaa6602a2 — **fixed** (C1)
Judge directive followed: raising `@trust_boundary(source_param="content")`
mirroring the adjacent `_verify_content_bytes` decorator, with the existing
test `test_verify_signed_manifest_bytes_rejects_non_dict_json` as `test_ref`
and the gate-emitted fingerprint. Related per-line entries → delete.

### contracts/type_normalization.py:R5:classify_runtime_type:fp=17534a078b1a1ad8 — **fixed** (C1)
### contracts/type_normalization.py:R5:classify_runtime_type:fp=48d40e586998365d — **fixed** (C1)
### contracts/type_normalization.py:R5:classify_runtime_type:fp=76805a78ab901edb — **fixed** (C1)
The provenance the three BLOCK-PENDING verdicts could not see is declared: the
function's own docstring states it exists for `SchemaContract.validate()` over
Tier-3 row values ("exotic types should be quarantined, not crash the run …
Never raises"). One `@trust_boundary(source_param="value", non_raising=True)`
retires all three; the caller producing `TypeMismatchViolation` is
contracts/schema_contract.py:288.

## core/

### core/canonical.py:R5:_normalize_value:fp=49833e42b57350cd — **restage**
(Current fp after drift: `70798bff8a7cfdf0`, line 118.) The judge asked for an
accurate site-specific rationale, rejecting the earlier precision-loss claim.
The honest claim is dispatch order, not precision.
**Proposed rationale:** "`_normalize_value(obj: Any)` is the audit
canonicalizer's heterogeneous union dispatcher; `pd.Timestamp` is a
`datetime.datetime` subclass, so this arm MUST precede the generic
`isinstance(obj, datetime)` arm (canonical.py:130-133) to apply the
pandas-specific tz policy (`tz_localize('UTC')` for naive values vs the
datetime arm's `replace(tzinfo=UTC)`); it makes no precision claim. Every
unrecognized type falls through to the arms below and ultimately to the
non-finite/unsupported-type ValueError raises this module documents — nothing
is defaulted or suppressed. Nominal dispatch over a declared third-party
type in a canonical serializer, not a re-check of an owned contract."

### core/canonical.py:R5:sanitize_for_canonical:fp=1bae438efdc3281a — **fixed** (C3)
### core/canonical.py:R5:sanitize_for_canonical:fp=1cb4a1a5f3318a20 — **fixed** (C3)
### core/canonical.py:R5:sanitize_for_canonical:fp=54a12b556b0dfa3d — **fixed** (C3)
### core/canonical.py:R5:sanitize_for_canonical:fp=5fe8990fa2ff266f — **fixed** (C3)
### core/canonical.py:R5:sanitize_for_canonical:fp=79be1076757324d5 — **fixed** (C3)
One `@trust_boundary(source_param="obj", non_raising=True)` on the quarantine
sanitizer retires all five, exactly per the judge's structural directives.
Related per-line entries → delete.

### core/checkpoint/recovery.py:R6:RecoveryManager:get_resume_point:fp=631ad1dc3385fe95 — **abandoned** (C4)
(Drift repair; live fp was `46f585fcde10c3eb` at line 798.) Judge verified the
defect: `get_latest_checkpoint` is a raw persistence read that returns a
checkpoint or None and raises `CheckpointCorruptionError` on malformed data —
it can never raise the caught `IncompatibleCheckpointError`. The handler was
dormant fail-open code (it would have converted a real incompatibility signal
into a silent "no resume point"). Deleted outright; corruption now propagates,
pinned by the new `test_get_resume_point_propagates_checkpoint_corruption`.

### core/config.py:L1:_module_:fp=a826e4d2cad76f47 — **restage**
The violation is real (judge: GENUINE) and the fix is a cross-layer
restructure now tracked as **elspeth-2ce6beb930** (filed by this lane,
mirroring elspeth-a881fce8bc): move the EmittedToOutput declaration surface
downward or inject the resolver. Not fixable inside this lane without a
plugins/-crossing refactor colliding with sibling lanes. **Operator note:**
the judge ruled architectural motivation does not justify an L1 allowlist
entry, so a restage will likely BLOCK again — this entry needs an operator
adjudication (accept as ticketed debt, or schedule elspeth-2ce6beb930 first).
**Proposed rationale (honest, does not over-claim):** "Known L1→L3 inversion:
`_declared_emitted_options` function-locally imports
`plugins.infrastructure.manager.get_shared_plugin_manager` to read live
EmittedToOutput declarations for the pre-expansion env-placeholder security
gate. ADR-006 Violation #11 remedies were walked in
docs/agents/sweeps/tier-burndown/B03.rationales.json and each makes the
guard worse (a hand-maintained core catalog is the SILENT FAIL-OPEN shape the
guard replaced). Structural fix is scoped as elspeth-2ce6beb930; this entry
asserts only that the import is confined to that one function and feeds only
the fail-closed rejection path."

### core/config.py:R1:_lower_llm_profile_nodes:lower_component:fp=40eeea1cb560a430 — **fixed** (C2)
### core/config.py:R1:_lower_llm_profile_nodes:lower_component:fp=5b6888591b31f35b — **fixed** (C2)
### core/config.py:R5:_lower_llm_profile_nodes:lower_component:fp=4d7b11b4182caa5b — **fixed** (C2)
The nested closure is hoisted to module-level `_lower_llm_component(component,
*, location, profiles, materialize)` under a raising
`@trust_boundary(source_param="component", suppresses=("R1","R5"))` — the
structural form the judge prescribed (the outer decorator on
`_lower_llm_profile_nodes` could not root a nested function's parameter).
Pinned by the new
`test_lower_llm_component_rejects_non_string_profile_alias` (non-str alias,
profile+provider conflict, unknown alias each raise ValueError) with
gate-emitted fingerprint. Note: the 4d7b11 worklist rationale (yaml `or {}`)
was mis-paired; that defect is the dc1233 entry below and is also fixed.

### core/config.py:R5:_expand_env_vars:_expand_value:fp=8ed2d3cce60483f6 — **fixed** (C2)
Hoisted the nested `_expand_value`/`_expand_string` closures to module-level
`_expand_env_value`/`_expand_env_string`; `_expand_env_value` carries the
raising `@trust_boundary(source_param="value")` the judge prescribed, pinned
by `TestExpandEnvValueBoundary::test_missing_env_var_without_default_raises`.

### core/config.py:R5:_fingerprint_secrets:_process_value:fp=1cb539be91b0aab2 — **fixed** (C2)
### core/config.py:R5:_fingerprint_secrets:_process_value:fp=6bef2dbeff8d914c — **fixed** (C2)
### core/config.py:R5:_fingerprint_secrets:_recurse:fp=0b6fb8d9373d8462 — **fixed** (C2)
Both fingerprinting walkers hoisted from closures to module level
(`_process_value`, `_recurse` — the latter keeps its name because
contracts-whitelist dict-pattern entries `_recurse:d`/`_recurse:return` bind
to it) under raising `@trust_boundary` contracts. The judge's split verdicts
(one non-raising, one raising) resolve to RAISING: `_process_value` raises
`SecretFingerprintError` control-dependent on a value-derived guard
(secret-with-no-key), and `_recurse` raises on fake-fingerprint collision.
Pinned by `TestFingerprintSecretsBoundary` (no-key refusal, dev-mode
passthrough, collision), gate-emitted fingerprints.

### core/config.py:R6:_fingerprint_secrets:fp=7227bac24a624dd8 — **fixed** (C1+C2)
Judge verified the guard did not derive from its authority. The
`try get_fingerprint_key() / except ValueError` probe is replaced by the new
`contracts/security.fingerprint_key_available()` predicate, which implements
exactly the two absence conditions `get_fingerprint_key` raises for
(membership + emptiness) — no exception swallowing anywhere on the path.

### core/config.py:R5:_sanitize_dsn_option_for_audit:fp=b657b0931e8c85b8 — **fixed** (C2)
Raising `@trust_boundary(source_param="options")`: a DSN option carrying a
password with no fingerprint key raises `SecretFingerprintError` (pinned by
`TestSanitizeDsnOptionBoundary`), while absent/non-str values are deliberately
left for plugin config validation.

### core/config.py:R5:load_settings:fp=dc1233340d69f224 — **fixed defect + restage of surviving site** (C2)
The real defect behind this entry (per the correctly-matched judge text):
`yaml.safe_load(_f) or {}` silently converted falsy non-mapping documents
(false / 0 / [] / "") into a valid-looking empty mapping. Fixed: only `None`
(empty document) maps to `{}`; every other non-dict document now hits the
ValueError. Pinned by `TestLoadSettingsYamlDocumentShape` (parametrized
false/0/[]). The isinstance itself is the honest boundary check on file
content (not rooted at a parameter — decorator inapplicable, as the judge
noted) and survives as fp `08023f562b578fc6` (line 3432).
**Proposed rationale for the surviving site:** "`load_settings` re-reads the
settings file directly (bypassing Dynaconf) to reject unknown YAML keys;
`_yaml_only` is freshly parsed operator-authored file content with no shape
guarantee. The check raises ValueError naming the file and actual type for
every non-mapping document — including falsy ones, since the loader maps only
a None (empty) document to {} (pinned by
tests/unit/core/test_config.py::TestLoadSettingsYamlDocumentShape). Value is
file content loaded inside the function, not a parameter, so the structural
decorator cannot apply."

### core/config.py:R5:load_settings_from_yaml_string:fp=daf4b2c90d5b6998 — **fixed** (C2)
This key's site is the `isinstance(config_dict, dict)` rejection of
web-authored YAML (the worklist rationale text about `ast.Name` was
mis-paired; that defect lives in expression_parser and is fixed there).
Raising `@trust_boundary(source_param="yaml_content")` per the structural
criteria, pinned by
`TestLoadSettingsFromYamlStringBoundary::test_non_mapping_yaml_document_raises`.

### core/expression_parser.py:R5:_ExpressionValidator:_is_none_constant:fp=88009e5aeecc5111 — **fixed** (C3)
Two defects in one: (a) the dormant `ast.Name(id="None")` arm — impossible
since Python 3.8 and FAIL-OPEN if the grammar ever changed (it would ADMIT the
new construct) — is deleted; (b) the surviving `ast.Constant` check sits under
a non-raising `@trust_boundary(source_param="node")` on the method (the
validator records errors, never raises).

### core/expression_parser.py:R5:_ExpressionValidator:visit_Subscript:fp=306c4f624d92c7b9 — **fixed** (C3)
### core/expression_parser.py:R5:_ExpressionValidator:visit_Constant:fp=c20113ee6198e154 — **fixed** (C3)
### core/expression_parser.py:R5:_ExpressionValidator:visit_Constant:fp=d26d7ab5da0fa27c — **fixed** (C3)
Non-raising `@trust_boundary(source_param="node")` on each recorded-error
visitor method, per the structural directives (`self.errors.append`, never a
raise).

### core/landscape/database.py:R1:LandscapeDB:_configure_sqlite:_emit_begin:fp=e68905c6e519fc47 — **restage**
Drift repair only: the site moved (now line 1108, current fp
`a9a9e13d637812fc`); the previously ACCEPTED reasoning still holds and the
mis-paired worklist text (checkpoint manager) does not concern this site.
**Proposed rationale:** "`_emit_begin` reads
`conn.get_execution_options().get(WRITE_INTENT_OPTION, False)` — an optional
SQLAlchemy execution option on a third-party mapping where ABSENCE is the
documented plain-transaction path (the paired writer `begin_write` sets the
option immediately before `conn.begin()`; database.py:230-260). Optional
extraction at a third-party boundary, not Tier-1 audit data; if this site
ever reads Landscape rows/checkpoints/audit JSON with .get() the suppression
is invalid."

### core/landscape/database.py:R5:_safe_database_descriptor:fp=2a2018db6258bb8e — **fixed** (C4)
Non-raising `@trust_boundary(source_param="connection_string")` per the
(correctly-matched) structural directive: the value union `str |
tuple[str, ...]` comes from SQLAlchemy's parsed URL query of an
operator-supplied connection string; unparseable input returns the redacted
sentinel. (The worklist text attached to this key — journal `_columns_to_values`
— was mis-paired and does not describe this site.)

### core/landscape/execution/node_states.py:R5:NodeStateRepository:complete_node_state:fp=113ee5ea3945332c — **restage**
Drift repair: the union widened to include `RowUnionFailureReason` (current fp
`b95125493bc11b9d`, line 468), which invalidated the old entry's "exactly two
dataclass variants" caveat. The pattern itself is the explicitly-permitted
form: nominal isinstance against concrete OWNED classes for genuine union
dispatch.
**Proposed rationale:** "`complete_node_state`'s `error` parameter is the
declared closed union `ExecutionError | TransformErrorReason |
CoalesceFailureReason | RowUnionFailureReason | None`
(node_states.py:429). ExecutionError, CoalesceFailureReason and
RowUnionFailureReason are owned frozen dataclasses with `to_dict()`;
TransformErrorReason is a TypedDict (already a dict). The flagged isinstance
selects which union members need reification before canonical audit
serialization — genuine nominal union dispatch over owned concrete classes,
not a defensive re-check. Valid only while the signature remains this closed
union and the tuple lists exactly the `to_dict()` dataclass variants."

### core/landscape/execution_repository.py:R6:ExecutionRepository:complete_aggregation_result:fp=2a4225af8a3ecf95 — **fixed** (C4)
Judge verified (via the correctly-matched 306c4f text): the post-failure probe
discarded a detected `AuditIntegrityError`. The probe is now
`_probe_existing_aggregation_receipt` returning the declared
`_ReceiptProbeOutcome` (MATCH / NO_MATCH / PROBE_UNAVAILABLE); its handler
catches ONLY `SQLAlchemyError`, so receipt-divergence `AuditIntegrityError`
propagates with the original write failure as context. Pinned by
`TestAggregationReceiptProbeOutcome` (4 tests, including the propagation
case).

### core/landscape/journal.py:R5:LandscapeJournal:_normalize_parameters:fp=a9d8a59a2223776d — **fixed** (C4)
### core/landscape/journal.py:R5:LandscapeJournal:_normalize_parameters:fp=d4a7aeb000edbb24 — **fixed** (C4)
Per the correctly-matched directive (attached in the worklist to the 2a4225
key): `parameters` arrives from SQLAlchemy's `after_cursor_execute` listener —
third-party-shaped containers, a structural Tier-3 boundary. Raising
`@trust_boundary(source_param="parameters")` (AuditIntegrityError via
`serialize_datetime` for non-finite floats), pinned by the new
`test_non_finite_float_parameter_raises` with gate-emitted fingerprint.
(The worklist texts attached to these two keys — "rehydrated Tier-1
resolution_json" — belong to run_lifecycle below.)

### core/landscape/run_coordination_repository.py:R6:_record_best_effort_event:fp=9a4208c0d0f37f5f — **fixed** (C4)
Judge verified (drift-paired): every SQLAlchemyError from a Tier-1 write was
swallowed with only a log line. The writer now returns the declared
`BestEffortEventOutcome` (RECORDED / LOST_TO_DB_FAULT): the failure is
recorded (logged at the same WARNING/ERROR split) AND surfaced as the
function's declared failure result — the R6 doctrine form — while the
load-bearing "never raises" property on the fence-refusal and
heartbeat-degraded paths is preserved and pinned
(`TestBestEffortEventOutcome`, plus the pre-existing no-raise thread tests).

### core/landscape/run_lifecycle_repository.py:R5:RunLifecycleRepository:_parse_field_resolution_mapping:fp=2ad034f8b0bfcf72 — **fixed** (C4)
### core/landscape/run_lifecycle_repository.py:R5:RunLifecycleRepository:_parse_field_resolution_mapping:fp=4148263c815d632e — **fixed** (C4)
Per the correctly-matched verdicts (attached in the worklist to the journal
keys): rehydrated first-party audit JSON must not be isinstance re-checked.
Rewritten to direct access with the informative `AuditIntegrityError` DERIVED
from the natural failure (`KeyError`/`TypeError` on the envelope,
`AttributeError` on `.items()`), the admitted hypothetical-deserializer key
re-check deleted, and the value check reduced to the writer's exact-type
contract (`type(value) is not str` — house style for Tier-1 exactness, cf.
contracts/call_data `_require_string_mapping`). Every pinned corruption test
(non-dict JSON, array JSON, missing key, non-dict mapping, non-str/null
values, unparseable JSON) passes unchanged.

### core/llm_profiles.py:L1:_module_:fp=1f68a8babc58e0f6 — **restage**
Real violation with a previously-adjudicated, still-open fix ticket:
**elspeth-a881fce8bc** ("Split LLM provider config models from their
httpx/AuditedHTTPClient runtime clients so core/llm_profiles.py can drop its
L1 upward import", residual of B04, rationalised in
docs/agents/sweeps/tier-burndown/B04.rationales.json). The split crosses
plugins/transforms/llm/providers/* — outside this lane and colliding with
sibling lanes. **Operator note:** same caveat as the config.py L1 — the judge
ruled architectural debt does not justify an L1 entry, so this needs operator
adjudication (accept as ticketed debt or schedule a881fce8bc first).
**Proposed rationale (honest):** "Known L1→L3 inversion:
`LLMProfileSettings._validate_provider_binding` imports
`LLMTransform.discriminated_variants()` because profile validation must
consume the SAME provider registry the runtime uses (a second allowlist is
the divergence this validator exists to prevent), and the provider config
models are currently fused with their runtime clients. Structural fix is
scoped as elspeth-a881fce8bc; this entry asserts only that the import is
function-local to the validator and adds no new upward surface."

### core/schema_shape.py:R5:_normalize_postgresql_operator_classes:fp=dc496a1af3456ca0 — **fixed** (C3)
Judge verified the mixed-provenance defect (via the correctly-matched 4148
text): the same helper served declared Tier-1 Index metadata AND reflected
Tier-3 database state, converting a Tier-1 wrong type into an ordinary schema
difference. Split by provenance:
`_normalize_declared_postgresql_operator_classes` (Tier-1 — crashes via
direct `.items()`) and `_normalize_reflected_postgresql_operator_classes`
(Tier-3 — keeps the `('<invalid>', repr)` sentinel under a non-raising
`@trust_boundary(source_param="value")`).

### core/schema_shape.py:R5:_proven_pg_catalog_text_builtin_calls:fp=29ff517bec3c1977 — **fixed defect + restage of surviving sites** (C3)
Judge verified (via the correctly-matched 1f68 text): the `else` arm conflated
the valid `None` bind with impostor types and silently degraded both to the
empty proof. Now an exhaustive `Connection / Engine / None / assert_never`
dispatch — an impostor crashes. The two surviving dispatch arms are new fps
`7d9a5c717a246c24` (line 725) and `d573da22dee79d0e` (line 727).
**Proposed rationale for both:** "Exhaustive nominal dispatch over
SQLAlchemy's declared `Engine | Connection | None` union for
`inspector.bind` (third-party concrete classes): each arm selects the
connection-acquisition strategy, the `None` arm alone degrades to the empty
proof, and the closing `assert_never(bind)` crashes on any impostor — no
conflated fallback remains (fixed in lane elspeth-ca0a7e71b1, commit
fbb2c8392)."

### core/secrets.py — all 15 findings **fixed** (C3)
`parse_secret_ref_marker` fp=135ebbddde8030bf, fp=1078580189025a46 ·
`_walk_redact` fp=f9673394dd035894, fp=8f384610036e7b0e ·
`_is_secret_env_ref` fp=2e7e27c70a984749 · `is_wired_secret_value`
fp=9ae3ef9da996f1ae · `_collect_credential_field_violations`
fp=29c6441a4be07fc0, fp=52427f0748b0e2a3, fp=bbedceb47465c201,
fp=c11906e87454b7da · `_collect_secret_ref_marker_sites`
fp=1045d7fbb4d67ab4, fp=885583b5183ec69f, fp=cd5534410ba60d72 ·
`_walk` fp=5ddcd129f2854b0b, fp=cd6789c169ff2027.
All seven functions are recursive walkers over web-/YAML-authored config trees
rooted at their own parameter — exactly the structural Tier-3 boundary case in
every attached verdict. Each now carries a non-raising
`@trust_boundary(source_param=…, suppresses=("R5",))`; `_walk`'s unresolvable
refs remain accumulated in `missing` and surfaced by the caller's
`SecretResolutionError` (unchanged). All related per-line entries → delete.
(The worklist texts mis-paired onto the two `parse_secret_ref_marker` keys —
templates `_literal_kwarg_values` reachability — are addressed at their true
site below.)

### core/templates.py:R5:_record_dynamic_attribute_filter_access:fp=f8afc7c1ffa60562 — **fixed** (C3)
Non-raising `@trust_boundary(source_param="node")`: a non-literal `attr`
argument records `ATTR_FILTER_DYNAMIC_ACCESS` rather than being coerced or
dropped. (The attached decode_sink_effect text was mis-paired.)

### core/templates.py:R5:_macro_row_splat_targets:fp=c0c0d558b6c88e9a — **fixed** (C3)
### core/templates.py:R5:_macro_row_splat_targets:fp=6194561c4d6abf93 — **fixed** (C3)
### core/templates.py:R5:_macro_api_splat_targets:fp=d459a9e520e22b87 — **fixed** (C3)
### core/templates.py:R5:_callblock_argument_bindings:fp=851cd9db04599fa3 — **fixed** (C3)
### core/templates.py:R5:_callblock_api_splat_targets:fp=f223bed7473c4d29 — **fixed** (C3)
Judge verified the class (851cd9/f223be/135ebb texts): `Macro.args` and
`CallBlock.args` are jinja2's DECLARED `List[Name]` (parser grammar; verified
against jinja2 3.1.6 source), so `if isinstance(target, Name)` filters were
dormant re-checks whose only possible effect was to SILENTLY DROP a
dynamic-access/binding report — the unsafe direction for a security analysis.
All filters removed; corruption now crashes via `.name`. The same defect at
the two SIGNED sibling sites in `_callblock_row_splat_targets` is fixed in the
same commit (honest fix over signature churn — John's standing ruling); their
signed entries (fps `25b39af94ff739f0`, `b9ef34ce9f379ae9`) become dangling.

### core/templates.py:R5:_literal_kwarg_values:fp=1d78f2face606017 — **fixed** (C3)
### core/templates.py:R5:_literal_kwarg_values:fp=bdbec3477c53e74e — **fixed** (C3)
Non-raising `@trust_boundary(source_param="node")` per both attached verdicts.
The invariant text deliberately claims ONLY the omission behavior ("returns
the statically traceable subset") — the judge separately verified that the
`_has_unknown_kwarg_values` pairing does NOT hold on the
`_macro_argument_bindings`/`_callblock_argument_bindings` paths, so no
rationale here relies on it. Whether those binding paths should also consult
the detector is an analyzer-semantics question left for adjudication (see
open questions below).

### core/templates.py:R5:_row_api_container_entries:fp=fb29f2a385c2d334 — **fixed** (C3)
Non-raising `@trust_boundary(source_param="node")`: Dict-literal keys in a
template are arbitrary expressions; only literal-string keys produce carrier
paths. (The attached runtime_checkable-Protocol text was mis-paired — no
Protocol dispatch exists at this site.)

---

## Surviving derivative sites needing per-line entries (not among the 77)

| current key | proposed rationale |
|---|---|
| `contracts/results.py:R5:_require_artifact_metadata:fp=5cd957a737428a01` | see ffb91c entry above |
| `core/config.py:R5:load_settings:fp=08023f562b578fc6` | see dc1233 entry above |
| `core/schema_shape.py:R5:_proven_pg_catalog_text_builtin_calls:fp=7d9a5c717a246c24` | see 29ff51 entry above |
| `core/schema_shape.py:R5:_proven_pg_catalog_text_builtin_calls:fp=d573da22dee79d0e` | see 29ff51 entry above |

## Shared-baseline changes needed

- **Masquerade / dynamic-attribute pinned sets: NO changes.** These commits
  add no `getattr`/`hasattr`/`inspect` probes (verified by grep over the
  diffs); the added `@trust_boundary` decorators are the gate's permanent
  structural amnesty, not new sites.
- **contracts-whitelist.yaml: NO changes.** The hoisted fingerprinting walker
  deliberately keeps the name `_recurse` so the existing
  `core/config.py:_recurse:d` / `_recurse:return` dict-pattern entries stay
  bound (`scripts/check_contracts.py` green). If the hub prefers a
  descriptive name (`_fingerprint_recurse`), rename it AND the two whitelist
  entries together.
- **tier_model allowlist drift wave (hub re-stage):** fingerprints are
  line-sensitive, so entries bound to surviving sites in the 22 edited files
  under `src/elspeth/{contracts,core}` will report drift and need
  sidecar-WINS drift repair at the next stage_scan. Known dangling entries to
  DELETE (site removed or now R_TB_SUPPRESSED): all per-line entries for the
  70 fixed findings above, plus signed
  `core/templates.py:R5:_callblock_row_splat_targets:fp=25b39af94ff739f0` and
  `fp=b9ef34ce9f379ae9` (sites removed), and signed
  `contracts/results.py:R5:_require_artifact_metadata:fp=4baf0185d7ea5e4a`
  (function body changed; the outer frozen check it covers is unchanged in
  meaning — drift repair, not delete).
- **Tracker:** filed **elspeth-2ce6beb930** (config.py L1 inversion), sibling
  to the pre-existing **elspeth-a881fce8bc** (llm_profiles L1). Both L1
  restages will likely block again absent operator adjudication — flagged in
  their entries above.

## Open questions for adjudication

1. The two L1 restage entries (above) — accept as ticketed debt or gate on
   the inversion tickets.
2. templates binding paths (`_macro_argument_bindings`,
   `_callblock_argument_bindings`) silently omit non-literal kwarg bindings
   without consulting `_has_unknown_kwarg_values`; the judge flagged the gap
   when it was used as a control-location claim. Fixing it means the alias
   analysis must taint untraceable bindings — an analyzer-semantics change
   this lane did not take unilaterally.
