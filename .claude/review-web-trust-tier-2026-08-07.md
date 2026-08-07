# Trust-tier / offensive-programming breach sweep — `src/elspeth/web`

Date: 2026-08-07 · Branch: `release/0.7.2` @ `b83ee118f` · Method: 8 sonnet finder agents by boundary
density, every P1/P2 re-verified personally against source by the lead. Read-only: no files modified,
no commits, no tests run.

---

## 0. GATE CORRECTION — READ THIS FIRST

**`elspeth-lints check` runs ZERO rules and always exits 0.**

`elspeth-lints/src/elspeth_lints/core/cli.py:315` declares `--rules` with `default="nothing"`, and
`_parse_rules("nothing")` returns `()`, so `_run_check` short-circuits at line 1260-1262:

```python
requested_tokens = _parse_rules(args.rules)
if not requested_tokens:
    return _emit_findings([], output_format=args.format, rules=[])   # exit 0, no rules executed
```

`AGENTS.md` documents the bare command as the gate:

```bash
elspeth-lints check            # static-analysis / trust-tier lint gate
```

Anyone — human or agent — who follows the project's own quick reference to verify their work gets a
green result that proves nothing. This is exactly the inert-gate trap `AGENTS.md` warns about for
Wardline (`--fail-on-inert`), reproduced in the lint gate with no inert detection. **I fell into it
myself in this session** and used the false green as a load-bearing premise; two conclusions I gave
earlier were wrong and are retracted in §5.

`--rule-set {static,full}` is also a dead flag: `rule_set` appears nowhere in the codebase outside its
own argparse declaration.

**Real enforcement is fine.** Pre-commit wires 13 rules with explicit `--rules` + scoped `--root`,
including `.pre-commit-config.yaml:172`:

```
check --rules trust_tier.tier_model --root src/elspeth
```

So this is a documentation + CLI-ergonomics defect, NOT "CI is inert". Recommended fixes: make the
default rule set "all registered rules" (keep `nothing` as the opt-in skeleton), or correct AGENTS.md's
quick reference to the real invocation. Ideally both.

### What the gate actually says when run correctly

```
env PYTHONPATH=elspeth-lints/src ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/python -m elspeth_lints.core.cli check --rules trust_tier.tier_model --root src/elspeth
```
→ **exit 1**, 2631 finding lines, **2285 of them under `web/`** (R1 943, R5 855, R6 137, R4 76, R7 40,
R8 30, R2 27, R9 16).

This is the deliberate fail-closed state AGENTS.md describes, and the corpus is already tracked as
milestone `elspeth-13f0cc04fb` ("Remediate the release/0.7.2 trust-tier finding corpus"). It is **not**
2285 newly discovered breaches — do not report it as such.

Two gate warnings worth acting on:
- `allow_hits[159]` binds to `web/composer/guided/steps.py`, a deleted file — the loader says
  **"Refusing to load."** One of the orphaned entries in §4 actively breaks allowlist loading.
- Per-file `max_hits` ceilings are exceeded: `guided/chat_solver.py` **36/11**, `tools/_common.py`
  15/10 and 20/13, `tool_batch.py` 5/3. Ratchets that have slipped.

Wardline, by contrast, was run correctly and is genuinely clean:
`wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
→ exit 0, PASSED, 66 recognized boundaries, **zero findings of any severity under `src/elspeth/web`**.

---

## 1. Highest-value findings (all personally verified)

### P1 — Two signed allowlist entries name a producer-side gap as their own invalidation condition, and it is still open
`src/elspeth/web/composer/state.py:157-165` (`SourceSpec.from_dict`; same in `NodeSpec`/`OutputSpec`)

```python
return cls(plugin=d["plugin"], on_success=d["on_success"], options=d["options"], ...)
```

`options` is stored verbatim into a field typed `Mapping[str, Any]`; `deep_freeze` passes non-containers
through unchanged. `config/cicd/enforce_tier_model/web.yaml:2201-2238` (ACCEPTED, expires 2026-10-06)
states in its own reason text that "a non-Mapping source.options is a **REACHABLE Tier-3-origin state**"
via both session reload and the HTTP seed endpoint (`routes/composer/state.py:487`, body typed
`state: dict[str, Any]`), and that it is "**Invalidated if** SourceSpec.options is shape-validated as a
Mapping at from_dict/construction... at which point this guard becomes dead and should be dropped."
A second entry (`fp=5bfb2b8decc7e26e`) says the same for `NodeSpec.options`. The judge accepted at
confidence 0.72 while explicitly noting it could not verify the load-bearing fact.

The consumer-side guards are honest; the defect is that the hole they compensate for has been
documented and left open since 2026-07-08. **Fix `from_dict`, then delete both guards and both entries**,
exactly as their own text instructs.

Confirmed downstream consequences: `execution/service.py:1465-1471` calls `UUID(blob_ref)` where
`UUID.__init__` calls `.replace()` before any type check, so a non-str raises `AttributeError` and
escapes the `except ValueError`, bypassing the designed `MalformedBlobRefError` quarantine (the same
file guards correctly 600 lines earlier); `service.py:8051-8063` `.get()`s on `node.options` while
decorated `@observation_boundary(invariant="...never raise")`; `service.py:1188` carries a comment
asserting a "Tier-1 dataclass invariant — no isinstance probe needed" that is false.

### P1 — Tier-1 blob integrity evidence discarded, captured nowhere
`src/elspeth/web/blobs/routes.py:286-290` and `:325-329`

```python
except (BlobIntegrityError, BlobContentMissingError):
    raise HTTPException(status_code=500, detail="Blob content integrity verification failed") from None
```

Both classes' docstrings (`contracts/blobs.py:328-361`) state this is "a Tier 1 integrity violation...
Callers must propagate it rather than batching or suppressing it", and both carry blob_id / expected /
actual hash / storage_path. Verified: `grep -c "slog\.\|logger\.\|logging\."` returns **0** for both
`blobs/routes.py` and `blobs/service.py` — the module contains no logging call of any kind, so a
content-hash mismatch on a stored blob is undetectable after the fact. Withholding detail from the
client is correct; the absence of server-side capture is the defect.

### P1 — `InvariantError` ("always a server bug") rendered as a user-blaming message
`src/elspeth/web/sessions/routes/composer/guided_chat_atomic.py:1483` and `:1520`

Caught alongside `PluginConfigError`/`TypeError`/`ValueError` and answered with "I couldn't apply that
uploaded file to this step". Its own docstring (`composer/guided/errors.py:33-38`) says it "is always a
bug in the server code, never a client error" and must surface as HTTP 500. Drop it from the tuple;
the outer handler already classifies it as `integrity_error`.

### P2 — A signed entry's stated invalidation condition has already been met by drift
`src/elspeth/web/app.py:342` (allowlisted) and `:506` (not allowlisted)

The entry (judged 2026-07-08T12:30:55) quotes `except (SQLAlchemyError, OSError)` and ends: "**Drift that
broadens this catch, removes the explicit error, or starts catching audit-corruption/invariant exceptions
would invalidate this suppression.**" Live code is now
`except (SQLAlchemyError, OSError, SchemaCompatibilityError)`. `SchemaCompatibilityError` is raised
"when the Landscape database schema is incompatible with current code" — an audit-corruption signal.
`git log -S` puts the addition in `0d25d1cc6`, dated **2026-07-13**, five days after the verdict. The
fingerprint binding did not trip.

### P2 — others confirmed against source
- `composer/tool_batch.py:1657` — advisor handler's `except Exception` lacks the
  `(AssertionError, MemoryError, RecursionError, SystemError): raise` carve-out its sibling at `:2058`
  documents as "DOCUMENTED DIVERGENCE ... NOT relaxed". Tier-1 invariant failures become "ADVISOR_ERROR".
- `sessions/service.py:11600-11603` — blanket `except IntegrityError` over a whole Tier-1 write chain
  containing no INSERT and no matching constraint; relabels corruption as a retryable conflict. The
  identical sibling at `:11605` has no such catch.
- `sessions/service.py:8811` — sole guarded read (`"sources" in row_mapping else None`) among siblings
  that all use direct attribute access; every other read of that column across the file is unguarded.
- `sessions/guided_payloads.py:35,58` + `guided_replay.py:189` — `isinstance(payload_store, PayloadStore)`
  where `contracts/payload_store.py:42-43` is `@runtime_checkable` — ADR-032's banned pattern, and the
  `:58` site gates a Tier-1 audit-settlement write. Previously unrecorded third instance.
- `_aws_ecs_acceptance/orphan_sweep.py:194` — `if response is None: return collected` treats a
  not-found on page 2+ as end-of-pagination, silently truncating the survivor count on a release gate.
- `execution/routes.py:1121-1127` — `ValueError → 404` swallows documented Tier-1 review-hash-drift
  raises from `materialize_state_for_execution` (`interpretation_state.py:1242, 1261, 1465, 857, 1085`),
  reporting an audit-attribution failure as "you asked for something that doesn't exist".
- `evidence.py:186-220` — `_verify_stored_receipts` proves listed receipts are consistent but never
  asserts a required (scenario_id, kind) set exists; a skipped receipt yields a shorter, self-consistent
  list that can still reach `phase="committed"`.
- `plugin_policy/compiler.py:81-82,111` — `getattr(..., None)` fallbacks on Tier-1 plugin-class
  attributes; sibling `catalog/service.py:184-185` does direct access with a "crash is correct" comment.

---

## 2. Rejected / downgraded (recorded so they are not re-raised)

- **Textract "vacuous true" receipt** — REJECTED. The reviewer claimed a zero-profile receipt is
  "textually identical" to a real pass. `_TEXTRACT_DETAIL_FIELDS` includes `profiles_configured`, which
  is carried and validated separately, so the receipt reads `profiles_configured: 0` and discloses its
  own vacuity. Naming nit at most.
- **`_guided_source_commit_failure_detail` leak (`_helpers.py:235-243`)** — investigated, NOT confirmed.
  It is dead code (call site deleted by `e83142e64`; only a unit test imports it). Searched
  `sessions/routes/` for any `detail=` carrying `tool_result` / `result.data` / a state repr: zero hits,
  and no other site references the `"Path violation (S2)"` text it redacted. No evidence of a live leak.
  This is a targeted-search negative, not an exhaustive trace. **Action: delete the dead function.**

---

## 3. Prior art — do not file duplicates

- **OIDC alg-header** (`auth/oidc.py:536-544`) is already `elspeth-e8a9973c37` (P2, **deferred**), with
  `elspeth-1484b45ec3` closed as its duplicate. Two things the tickets lack:
  1. The deferral premise is wrong. The ticket says "Currently BLOCKED by PyJWT 2.13.0's internal
     prepare_key guard (per verifier; NOT independently reproduced — read-only analysis), so not
     exploitable today." **I reproduced it**: forging `alg=HS256` against an RSA public-key object
     raises `TypeError: Expected a string value`, which is not a `PyJWTError` and escapes the handler at
     `:545`. The guard blocks signature *bypass*; it does not block the *crash*.
  2. Neither ticket names the **audit gap**: the uncaught `TypeError` skips
     `_record_auth_failure_after_rate_limit` (middleware.py:141,152 are the only call sites), and
     `app.py` has no generic `Exception` handler — so this class of hostile auth traffic leaves no
     Landscape record. That is the argument for un-deferring.
  Note `elspeth-fb62819858` (closed) made alg-less JWKS acceptance a deliberate interop fix, so any
  remediation must pin by key type (`kty`), not by requiring the JWKS to declare `alg`.
- Redaction `options` asymmetry (`redaction.py:3990-4003`) — near neighbour `elspeth-f9d7117738`
  (P2, deferred), same function, different aspect. Fold in rather than file new.
- `runtime_checkable` sites — fold into `elspeth-02cd60d8cd` (P1 step, pending).
- Orphan-sweep truncation, the `IntegrityError` catch, and the `sources` probe returned **0** tracker
  results — genuinely new.

---

## 4. Allowlist integrity (systemic)

Measured mechanically over `config/cicd/enforce_tier_model/web.yaml`: **248 entries, ≥11 hard-orphaned**
(cited file or leaf symbol absent). Stated as a floor — the check is a leaf-symbol substring match, so
relocated-body cases (e.g. `_reattach_guided_blob_refs`, now a stub) pass it.

`guided/steps.py:R6:_observed_columns_from_allowed_path` (whole file gone — the entry the loader refuses
to load) · `sessions/schema.py:_validate_named_checks` ×2 · `sessions/schema.py:_ddl_constraint_applies_to_dialect`
×2 · `composer/service.py:_cleanup_recipe_fast_path_blob` · `llm_response_parsing.py:_provider_method` ·
`routes/_helpers.py:_store_guided_audit_payload` · `routes/_helpers.py:_dispatch_guided_respond` ·
`composer/guided/audit.py:_redacted_validation_result` ×2

The sessions-routes reviewer reports ~25 of its 39 stale under a broader definition (function survives,
described mechanism rewritten — e.g. all 16 `guided.py` entries describe a `primary_exc`/`_persist_*`
architecture with zero occurrences today). Not mechanically verified; recorded as their claim.

Root cause: two refactors post-date the 2026-07-08/09 judge run with no allowlist re-scan —
`e83142e64` ("replace legacy guided proposal path") and `72f991d6a` ("Hide server paths in output labels").

**Worth a systematic pass:** grep every entry's reason text for self-stated invalidation clauses
("Invalidated if", "Drift that … would invalidate") and test each condition against the tree. Two of the
findings above were found that way by accident.

All of this clears through the agent-stages / operator-signs seam (`mcp__elspeth-judge__stage_scan`, then
the operator's `elspeth-lints sign-bundle`). **Never hand-edit an entry or a signature.**

---

## 4b. P3 backlog (as reported by the slice reviewers; NOT individually re-verified by me)

Carried here so the detail survives the session. Confidence is the reporting reviewer's.

**Composer** · `redaction.py:3990-4003` `_redact_one` silently tolerates a non-Mapping `options` while its
own 13-line comment argues the fail-closed case for the adjacent `source` check, and the sibling
`redact_guided_snapshot_storage_paths` (`:4118-4119`) crashes on exactly that shape (latent; needs a
serializer regression) · `planner_authoring_aids.py:1255,994` `.get()` on required `TypedDict` keys, and
`.get("kind") or .get("type")` letting an empty string fall through · `guided/state_machine.py:647`,
`guided/stage_subjects.py:147,554` `.get("kind")` discriminator dispatch contradicting their own
"Tier-1 strict: never `.get()`" docstrings · `guided/emitters.py:555-557` normalises a non-str `mode` to
`"observed"` · `guided/chat_solver.py:947` `except BaseException` around secondary audit recording ·
`guided/planning.py:962-990` silently retargets a dangling LLM-authored route to the sole output ·
`tools/_dispatch.py:592-596` `validate_arguments`/`require_data_dir_for_paths`/`raise_schema_argument_errors`
all default `False` on the single dispatch choke point (all 5 production callers verified passing `True`;
fail-open default is the footgun) · `tools/secrets.py:107-112`, `tools/sources.py:837-848`,
`tools/_common.py`/`transforms.py` over-wide `except (KeyError, TypeError, ValueError)`.

**Sessions** · truthiness rather than `is not None` on nullable UUID columns (`service.py:1682-1684,
5534-5536, 5573-5575, 5620-5622, 8337, 8340`) · `service.py:6961-6972` hand-walks
`interpretation_requirements` instead of the canonical `parse_interpretation_requirements` accessor whose
docstring says external readers "MUST come through here" · `_guided_step_chat.py:87-90` absorbs bare
`ValueError` as transient, contradicting `solve_step_chat_with_auto_drop`'s docstring ·
`schemas.py:331` `default_factory=list` inconsistent with the justified no-default sibling `chat_history`.

**Sessions routes** · three broad `except Exception` handlers that log ONLY for `AuditIntegrityError`,
bucketing genuine `TypeError`/`KeyError` bugs to `"operation_failed"` with no log anywhere
(`sessions.py:709-749`, `guided.py:1683-1716`, `guided.py:1934-1959`) · `pipeline_settlement.py:120-140`
surfaces Tier-1 audit self-inconsistency as a soft actionable 409 · `guided_chat_atomic.py:1810-1825`
`contextlib.suppress(Exception)` around the audit-FAILURE write with no log line.

**Execution** · `service.py:1264-1272, 1281-1289, 1306-1318` option values reach `Path(value)` with no
type assertion (fails closed — degrades error quality, not confinement; the three allowlist entries
judged `.get()` legitimacy, never this downstream gap) · `diagnostics.py:237-242` degrades Tier-1
`error_json` corruption to `None` while a sibling on the same column crashes (well-documented
best-effort heuristic, but never judge-adjudicated).

**AWS acceptance** · `orphan_sweep.py:308-321` infers "no telemetry expected" from an unset
`retained_evidence_path` · `textract.py:45-48`, `s3.py:81-89` unguarded `error.response` reads ·
ELSPETH-owned code wrapped in Tier-3 `except Exception → AcceptanceCheckError` relabels
(`bedrock.py:380-419`, `orphan_sweep.py:854-857`, `contracts.py:354-363`) · `evidence.py:312` hardcoded
`"verified": True` · `evidence.py:150-170` non-total terraform action switch ·
`http_client.py:277-285` `UnicodeDecodeError` escape (not attacker-reachable today).

**Top-level** · `config.py:74-75,855` `_allow_insecure_test_keys` weakens the secret-key-strength gate via
`"pytest" in sys.modules` — incidental process state, not an operator assertion (verified unreachable in
every shipped deploy config; outside what the R1 entry adjudicated) · `plugin_policy/validation.py:766-777`
dead `except RuntimeError` · `blobs/routes.py:189-224` inline blob creation trusts client-declared MIME
with no sniffing while the multipart path sniffs · `app.py:1797-1817` a *signed* rationale cites a
`Depends(get_current_user)` that does not exist (real mechanism is a manual `hmac.compare_digest`
bearer check — sound, but the signed prose is wrong).

**Also flagged, not orphan-detectable:** `web/execution/validation.py:R1/R1/R5:_reframe_settings_missing_parts`
(3 entries) binds to `validation.py`, but the function is now DEFINED in `_validation_diagnostics.py:45`
and merely imported into `validation.py:64`. My orphan scan does not catch this class — a substring match
succeeds on the import line. Concrete evidence that **≥11 is a floor, not a count**.

## 5. Retractions from my own earlier reporting this session

1. "`elspeth-lints check` → exit 0, zero findings, so every statically-expressible breach is already
   policed." **Wrong** — that invocation executed no rules. Run correctly the gate is red (§0).
2. "`core/schema_shape.py` is lint-clean." **Retracted** — 12 hits when the rule actually runs.
3. "The two post-refactor redaction helpers are not emitting new findings." **Retracted** —
   `composer/redaction` has 57 hits.

---

## 6. Coverage and gaps

- All 8 slices reported; every P1/P2 above re-read against source by the lead. P3s are listed as
  reported and were **not** individually re-verified — treat their confidence as the reviewer's, not mine.
- `web/frontend/` excluded: vendor `node_modules` only.
- `aws_ecs_acceptance.py` is an argparse shim; the substance is in `_aws_ecs_acceptance/`, covered.
- `web/interpretation_state.py` was assigned to and read by the top-level reviewer (boundary code
  closely, internal state-machine bodies skimmed) — but it was **not audited for its own compliance**,
  and it owns most of the hash-drift raise sites behind the execution finding. Two reviewers
  independently asked for it to get a dedicated pass. Same for `composer/guided/chat_solver.py`, the
  actual LLM tool-call parse boundary (and the file with the worst ceiling breach, 36/11).
- `composer/state.py` DAG/schema-contract propagation (~1000 lines) and `tools/_common.py` plumbing
  (~2000 lines) were grep-swept, not read closely.
- Nothing has been filed in Filigree. Recommended: annotate `elspeth-e8a9973c37` with the repro and the
  audit-gap evidence and correct its premise; fold the Protocol sites into `elspeth-02cd60d8cd`; file the
  three genuinely-new items; and raise the inert-gate defect (§0) separately — it is the one finding that
  affects every future verification in this repo.
