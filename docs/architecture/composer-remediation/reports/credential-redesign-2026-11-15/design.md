# Revised credential-detector design

This replaces the earlier design. It is based on read-only inspection of branch `integration/composer-remediation`, HEAD `362993b6f22d96dfcd8cdcba98973ca94f8257a9`. No files were edited, tests run, registry inventory executed, or stored data scanned.

## Release and lane summary

The smallest coherent first release is **CDA-26 alone**, fixing provider-error credential leakage without waiting for this redesign. The proposed allocation below puts the complete redesign in the next patch. Patch numbers are planning allocations, not authorization to edit package versions or release notes; the release-number reservation remains in `STITCHED-PLAN.md:121`.

Sizes include production code, tests, fixtures, and generated artifacts. Exact file manifests follow in each lane.

| Lane | Purpose | Principal files | Changed lines | Depends on | Proposed patch |
|---|---|---|---:|---|---|
| CDA-26 | Existing standalone provider-error leak fix | `llm_response_parsing.py`, its tests | Existing brief | Existing brief | 0.8.14 |
| CRED-A1 | Add immutable, versioned pattern authority; preserve v1 | Contracts catalogue and legacy adapter | 300–450 | CDA-26 integration coordination | 0.8.15 |
| CRED-A2 | Add bounded v2 evaluator and value-free findings, inactive | New matcher module | 450–600 | A1 | 0.8.15 |
| CRED-A3 | Generate browser catalogue; prove Python/JS parity | Generator, generated TS, browser evaluator, corpus | 450–600 | A2 | 0.8.15 |
| CRED-B1 | Separate field-name roles and inventory every registered config field | Core secrets/config, catalogue, provider schema, ECS helper | 450–600 | A1, A2 | 0.8.15 |
| CRED-B2 | Activate narrowing, version 2, and compatible readers together | Detector façade, guard, validators, evidence reader | 450–600 | B1, CDA-18 | 0.8.15 |
| CRED-B3 | Explicitly activate wider checking at user ingress only | User-input routes and schemas | 350–550 | B2; CDA-24/25 | 0.8.15 |
| CRED-C1 | Correct refusal transport, copy, guidance, and MCP classification | App handler, route helpers, dispatch, catalogue, MCP | 450–600 | B2; CTX-C; CDA-18 | 0.8.15 |
| CRED-C4 | Provide durable, trusted user notices | Result contract, notice grammar, finalization | 350–550 | C1; CDA-08/09; CTX-C | 0.8.15 |
| CRED-C2 | Add one bounded compose repair attempt | Compose service and loop carrier | 450–600 | C1, C4; CDA-18; CTX-A/B/C | 0.8.15 |
| CRED-C3 | Add planner repair feedback and propagate disclosures | Planner and planning application | 350–550 | C2, C4; CDA-08/09; CTX-A/B/C | 0.8.15 |
| CRED-D | Block browser paste/drop/send/fork before custody | Store, chat components, boundary tests | 400–600 | A3, B3, C1; CDA-08/24; CTX-C | 0.8.15 |
| CRED-E1 | Explain and bound Composer egress redaction | Validation, advisor projections, title context | 450–600 | B2, C1; CDA-07a/18 | 0.8.15 |
| CRED-E2 | Preserve structured audit evidence and carry reasons beside sentinels | Diagnostic evidence, visible tool evidence | 350–550 | B2, E1 | 0.8.15 |
| CRED-F | Make hook and CI consume the shared authority without leaking matches | Scanner CLI, hook, existing CI job | 300–500 | A2, B2 | 0.8.16 |
| CRED-G | Produce read-only stored-data impact reports before future widening | Reporting script and tests | 400–600 | B2, E2 | 0.8.17 |

**Merge order:** CDA-26 → A1 → A2 → {A3, B1} → B2 → {B3, C1, E1} → C4 → C2 → C3; D follows A3/B3/C1 and its frontend predecessors; E2 follows E1/B2. F and G follow separately. Resolve the external dependencies below before merging a lane touching their files.

**Parallel work:** A3 and B1 can run independently after A2. B3, C1, and E1 have separate production manifests after B2, but their shared test files need explicit assertion ownership. D, C2/C3, and E2 can run concurrently once their dependencies land. C2 and C3 merge sequentially. No parallel broad suites on the shared host.

**Release boundary:** Preparatory A lanes may merge independently while detector version remains `"1"`. Do not release the v2 activation without browser protection, explicit server ingress, compatible evidence readers, and the disclosure/feedback paths. Patch 0.8.16 adds repository scanning; 0.8.17 adds reporting only. Neither later patch widens replay, model-output, MCP-state, or engine admission.

## Design contract

### One authority, explicit purposes

The authoritative catalogue is Python data. The browser module is generated from it; neither browser code, hook code, nor the provider-error scrubber maintains another credential-pattern list.

Each catalogue record contains:

- Stable `id`, `family`, and `tier`.
- Applicable detector versions and purposes.
- Matcher kind: bounded token, portable regular expression, URL parser, assignment parser, or PEM marker.
- Character classes, length limits, boundaries, capture semantics, and precedence.
- Reference/placeholder handling where applicable.
- Verification provenance and status for vendor-specific formats.
- A shared test-recipe identifier.

Keep the entire v1 rule set permanently. In particular, its assignment rules, suffix rules, and URL interpretation remain available for historical evidence validation. An adapter continues exposing `CREDENTIAL_PATTERN_SPECS` while its existing consumers are migrated.

Use an explicit purpose argument:

| Purpose | Version-2 behavior |
|---|---|
| `control` — default for existing callers | Narrowing only: model output, provider-request history, replay, stored state, MCP state, engine contracts |
| `user_ingress` | Narrow rules plus the key-like tier and any newly verified vendor formats |
| `egress` | Narrow formats plus conservative assignment redaction; **no new vendor families or key-like widening** |
| `audit` | Narrow admission vocabulary, credential-field handling, and value-free projection metadata |
| Historical read with `detector_version="1"` | Frozen v1 semantics, irrespective of the current default |

A surface string identifies the producer for reporting; it must not select policy.

**The v2 control acceptance set must contain the v1 acceptance set.** Changing a regex, URL parser, field rule, or boundary must not quietly add control rejections. Resource-limit behavior remains explicit and is not presented as a credential match.

Wider ingress is safe for existing sessions because a rejected submission is stopped before storage and provider use. Existing persisted content is checked with `control`, never retrospectively with `user_ingress`. Tests must demonstrate this distinction using the same key-like string.

### Findings and safe positions

Extend `CredentialMaterialFinding` with:

- `tier`: `known_format`, `key_like`, or absent.
- `family`: a closed catalogue identifier or absent.
- `position`: an owned, value-free structural position and optional scalar span.
- Existing category, location, and detector version.

Positions use mapping-entry ordinals and sequence indices, not arbitrary key names. String spans are zero-based Unicode-scalar offsets with an exclusive end. JavaScript translates these to UTF-16 positions for selection/display.

Never include matched values, excerpts, prefixes, suffixes, arbitrary field names, or value hashes. Existing historical findings without these fields remain unknown; do not manufacture a family or position.

A credential-field rejection is described honestly as “a literal in a credential field.” A traversal limit is “the check could not complete.” Neither is falsely labelled a verified vendor-key match.

### Narrow formats and the key-like tier

Version 2 removes prose-only assignment detection from control admission. For example:

- `password: use the secret service` is allowed as prose.
- A configured `password` field containing a literal is still refused.
- A known-format key embedded in otherwise ordinary prose is still refused.
- Database credentials and nonempty HTTP userinfo remain subject to the existing credential-URL rule.
- Sensitive URL parameters require an actual non-placeholder value for v2 control admission; this only narrows the current name-based rule.

The proposed initial key-like predicate is deliberately bounded and reviewable:

- Scan maximal ASCII token runs, not every possible substring.
- Length 32–128.
- Alphabet limited to letters, digits, `_`, `-`, `+`, `/`, with separately bounded terminal padding.
- Require uppercase, lowercase, and digits.
- Require Shannon entropy of at least 4 bits per character.
- Exclude hexadecimal identifiers, UUIDs, recognized integrity-digest syntax, and exact supported references.
- Do not scan substrings of an oversized run to manufacture a qualifying token.

These are product heuristics, not vendor facts or a guarantee of credential detection. Keep them in catalogue data and test boundary cases in both runtimes.

**New vendor formats remain inactive until verified against official vendor documentation.** The reports’ proposed formats and parity measurements are useful leads, not verification performed by this design worker. Catalogue entries must distinguish inherited behavior from newly verified formats.

### Bounded evaluation

The existing JWT regex is at `credential_material.py:97`; malformed wire scanning reaches a 1 MiB allowance at `credential_guard.py:349–353`. The review’s timings were not remeasured here.

V2 tokenizes once and validates bounded candidates. It must not restart a JWT search at every `eyJ` prefix. Egress email detection receives the same bounded-candidate treatment; its current regex is at `validation.py:129–130`.

The shared adversarial corpus includes:

- Repeated `eyJa-` without terminating JWT separators.
- Long email-like runs without a valid domain.
- Repeated assignment labels and malformed URL candidates.
- Maximum-size safe input.
- A credential near the end of maximum-size input.
- Unicode surrounding a credential.
- Oversized tokens and oversized documents.

Run Python and JavaScript tests through 32 KiB, successive doublings, and 1 MiB. Use deterministic candidate/work bounds and generous serial time limits; a timing ratio alone is not the correctness oracle. An incomplete scan returns a distinct closed failure, never “clean.”

The original v1 evaluator remains available for historical semantics. Its metadata rereads are already limited to 512 characters at `diagnostic_evidence.py:192`; do not reapply unbounded legacy expressions to arbitrary new bulk input.

### Field-name roles

Replace the shared accidental coupling with explicit roles:

1. **Admission credential fields:** narrow exact names and specific credential suffixes.
2. **Secret-reference placement:** retain existing permitted placement for compatibility.
3. **Config fingerprinting:** retain the existing conservative redaction/fingerprint classification.
4. **Catalogue/provider schema projection:** use the admission classification and explicit plugin declarations.
5. **ECS environment screening:** preserve its conservative operator-facing policy.

The admission suffix set retains specific forms such as `_api_key`, `_secret_key`, `_private_key`, `_access_key`, `_secret`, `_password`, `_credential`, `_connection_string`, and token-specific forms including `_access_token`, `_refresh_token`, `_session_token`, `_auth_token`, `_id_token`, `_bearer_token`, `_sas_token`. Remove generic `_key` and `_token`.

Explicitly retain `aws_session_token`. Its real declaration is at `plugins/transforms/llm/providers/bedrock.py:147`; dropping `_token` without this replacement loses a credential field.

Expected examples:

| Field/value | Current admission | V2 admission | Other roles |
|---|---|---|---|
| `sort_key: "customer_id"` | Broad suffix can refuse | Allowed | Existing fingerprint/reference behavior preserved |
| `continuation_token: "next-page"` | Broad suffix can refuse | Allowed unless explicitly declared credential-bearing | Existing fingerprint/reference behavior preserved |
| `aws_session_token: "<literal>"` | Refused | Refused | Remains credential-bearing |
| `api_key: {"secret_ref": "service_key"}` | Allowed with valid reference policy | Allowed | Existing authorization applies |
| Ordinary field containing a known-format key | Refused | Refused | Value scanning remains active |

These are examples, **not a completed live-registry inventory**.

B1 must enumerate every config field from the live plugin manager, including nested schemas, variants, `$defs`, and references. Its before/after table records plugin, variant, field path, admission classification, reference placement, fingerprint classification, required-secret classification, and provider projection. Every registered field appears, including unchanged ones. Use the same live registry exposed at `web/catalog/service.py:220–251`, not source regexes.

Schema-declared credentials are desirable but cannot replace the current rule immediately. The code mixes plain `str`/`repr=False` declarations with `SecretStr`; `discovery_secret_requirements` at `catalog/service.py:519` is useful but not comprehensive. This release consumes existing declarations and preserves explicit exceptions. A comprehensive plugin-schema annotation rollout is a later, separate change.

### Copy and model feedback

Closed codes:

| Code | Meaning and guidance |
|---|---|
| `credential_material_rejected` | A known-format/key-like value or credential-field literal was refused; use a secret reference; rotate if real |
| `credential_scan_incomplete` | A resource/type boundary prevented a complete check; shorten or restructure the input |
| `credential_material_withheld` | Model-authored material was withheld from publication/replay |
| `credential_material_redacted` | A projection replaced credential material |
| `credential_egress_precaution` | A credential-labelled value was conservatively removed without asserting a known format |

Add direct guidance records to `tools/generation.py`. Do not add prose-matching regexes or modify the historical guidance fixture.

Copy must reflect custody:

- **User ingress:** “This text matches [known format / key-like rule] at [safe position]. It was not submitted to the model or stored as a message. Store the credential in Secrets and use a reference. Rotate it if it is real.”
- **Model output:** “The model returned credential material. That material was withheld from publication and replay.” Do not claim the provider never saw its own output.
- **Stored-state rejection:** “Stored content was excluded from this request.” Do not claim it was never stored.
- **Egress precaution:** “A credential-labelled value was removed before this projection was sent.” Do not claim a known vendor match.
- **Scan incomplete:** Explain the limit; do not advise rotation without a credential finding.

The exact feedback paths are:

| Report §8 surface | User channel | Model channel |
|---|---|---|
| Direct input refusal | Structured 422 plus browser inline alert | None: rejected user bytes must never reach a provider |
| Model prose/metadata refusal | Credential-specific failure or successful-repair notice | One bounded repair request containing safe code/family/position and catalogue guidance |
| Tool arguments | Structured tool failure when the protocol unit is admissible | Normalized tool result; otherwise repair from the last admitted history, without the rejected tool call |
| Planner candidate refusal | Final trusted notice or terminal refusal | Allowlisted planner feedback, using the direct guidance catalogue |
| MCP refusal | `ARG_ERROR` / semantic-rule response | The same safe response is returned to the MCP client; audit classification matches it |
| Advisor/title/context redaction | Durable notice where a user-facing result is affected | A fixed explanatory record adjacent to the redacted projection, outside untrusted quoted content |
| Audit redaction | Stable sentinel plus companion reason metadata | Only the safe projection and explanation may enter subsequent context |

Do not fabricate tool-result messages for invalid or withheld native tool-call IDs. Do not restore rejected reasoning/signature units to make a retry possible. CDA-18 owns the native-thinking custody rules.

A repair is at most **one additional physical provider attempt per logical operation**, shared across compose/planner repair paths. It consumes the existing deadline, quota, call cap, cancellation state, context-budget accounting, and audit machinery. A second credential refusal ends with safe disclosure. Context shedding must retain the current repair instruction and reason.

### Durable notice channel

The user-visible success channel is explicitly **`trusted_system_notice` in the persisted assistant response**, rendered by the existing `MessageBubble` segment handling at `MessageBubble.tsx:244–251`.

C4 adds a typed, value-free `credential_notices` result field and a canonical notice composer. Finalization folds credential facts into the existing single backend suffix, rebuilding from admitted `raw_assistant_content` when necessary. It must not append a second suffix or attribute server copy to the model.

The contract is enforced by `CONTRIBUTING.md:320–357`, `no_tool_policy.py:789`, and the existing rebuild behavior at `service.py:487–520`. Notice persistence must survive reload and same-turn receipt handling at `sessions/routes/_helpers.py:1720–1765`.

The result field is not a new database column. Existing response/receipt persistence carries the canonical notice. Never put rejected model text into `raw_assistant_content`.

## In-flight work and file ownership

The latest prompts take precedence over older embedded briefs. The inspected prep directories use README/ticket files and nested `prompt-head.md` files where root `plan.md`/`prompt.md` files do not exist.

| Existing work | Relevant actual/reserved files | Scheduling consequence |
|---|---|---|
| CDA-18 | Current edits include `contracts/composer_llm_audit.py`, `_compose_loop_carriers.py`, `audit.py`, `llm_response_parsing.py`, `provider_gateway.py`, `provider_thinking.py`, `tool_batch.py`, `credential_guard.py`, and related tests/assurance docs | B2 and C repair lanes follow it. Preserve its reasoning dispositions and audit accounting. |
| CDA-07a | `advisor_abandonment.py`, `advisor_context.py`, `composition_completion.py`, `planning_application.py`, `turn_effects.py`; advisor tests | E1 follows it; C3/C4 follow the completion/planning sequence. |
| CDA-08 | `composition_completion.py`, `planning_application.py`, `advisor_checkpoint.py`, `no_tool_policy.py`, `MessageBubble.tsx`, `chat.css`; conditional service work | C4 and D follow it. |
| CDA-09 | `composer_preflight.py`, service handoff/finalization, `composition_completion.py`, `no_tool_policy.py` | C2/C3/C4 follow the CDA-08 → CDA-09 sequence. |
| CDA-24 | Session schemas and Composer state route, frontend version/header/decoder/type work, API/store window, generation/skill work; conditional baseline predicate in `tool_batch.py` | B3, C1, and D rebase after its relevant changes. |
| CDA-25 | `sessions/routes/sessions.py`, `sessions/models.py` commentary, fork/error tests | B3 follows it. Do not reopen its snapshot policy. |
| CTX-A | `contracts/composer_llm_audit.py`, `context_budget.py`, `context_budget_history.py`, `pipeline_planner.py`, Composer `service.py`, shared census and tests | Serialize planner/service integration. |
| CTX-B | Audit contract, advisor transcript, context-budget/apply modules, `pipeline_planner.py`, Composer `service.py` | Same; repair uses the resulting budget APIs. |
| CTX-C | Above context paths plus `contracts/composer_progress.py`, `app.py`, `no_tool_policy.py`, reply refusal/progress, `composer_turn.py`, route helpers, frontend API/decoder/types/store/outcome UI | C1/C4/D follow it. No new progress reason is required here. |
| CDA-02b | Generation catalogue, Composer skill and inventory-related tests | C1 follows those edits. |
| CDA-03 | Plugin schemas/implementations and documentation | Run B1’s live-registry inventory against the final plugin tree. |
| CDA-19 | Earlier standalone detector narrowing | Superseded by this design; do not dispatch concurrently. |

Two review collision claims need correction:

- `.lanes/lane-01-CDA-07a/prompt.md:79–81` explicitly excludes `pipeline_planner.py` and assigns `MessageBubble.tsx` to CDA-08. The inspected working changes agree.
- `.lanes/lane-02-CDA-18/prompt.md:79–89` requires stable helper signatures and excludes `advisor_checkpoint.py`; planner/title call sites are consumers and potential integration conflicts, not confirmed current edits to all those files.

The schedule remains conservative around those consumers. These observations do not assert that any lane has merged.

**Shared assertions:** B2 owns detector-version and narrow-admission expectations in `test_diagnostic_evidence.py` and `test_visible_tool_event_persistence.py`. E2 owns new projection reasons and the invalid-JSON regression assertions. E2 rebases after B2; neither rewrites the other’s tests.

## Lane CRED-A1

**Purpose:** Introduce the single versioned catalogue without changing production decisions or the active detector version.

```json
{
  "ticket": "CRED-A1",
  "title": "Introduce immutable versioned credential catalogue without activation",
  "files": [
    "src/elspeth/contracts/credential_patterns.py",
    "src/elspeth/contracts/credential_material.py"
  ],
  "test_files": ["tests/unit/contracts/test_credential_pattern_catalogue.py"],
  "test_command": "pytest tests/unit/contracts/test_credential_pattern_catalogue.py tests/unit/contracts/test_credential_material.py -n 0 -q",
  "branch": "lane/CRED-A1",
  "worktree": "<repo>/.claude/worktrees/lane-cred-a1"
}
```

**Edits:** The JSON manifest plus the named tests. Move rule data into the catalogue; retain the existing public adapter and frozen v1 field/URL/reference rules.

**Must not edit:** Evidence readers, validators, engine callers, frontend, active version, provider accounting, or pattern behavior.

**Before → after:** The same input produces the same v1 finding and serialized output. Only rule ownership changes.

**Failing-first tests:** Catalogue completeness; unique IDs; explicit version/purpose membership; legacy adapter equality; retained v1 assignment behavior; active constant remains `"1"`. A missing catalogue entry and changed legacy pattern must fail the controls.

**Pins:** P1 and P7 below. No v2 payload or reader pin moves.

**Compatibility/operator effect:** None externally. Estimated 300–450 lines.

## Lane CRED-A2

**Purpose:** Implement the inactive bounded v2 evaluator, explicit purposes, positions, and projection-result types.

```json
{
  "ticket": "CRED-A2",
  "title": "Add bounded purpose-aware credential evaluator and safe findings",
  "files": [
    "src/elspeth/contracts/credential_patterns.py",
    "src/elspeth/contracts/credential_matching.py"
  ],
  "test_files": ["tests/unit/contracts/test_credential_matching.py"],
  "test_command": "pytest tests/unit/contracts/test_credential_matching.py tests/unit/contracts/test_credential_pattern_catalogue.py -n 0 -q",
  "branch": "lane/CRED-A2",
  "worktree": "<repo>/.claude/worktrees/lane-cred-a2"
}
```

**Edits:** Manifest, primary test, and shared recipe corpus `tests/fixtures/credential_matching_cases.json`.

**Must not edit:** The active façade’s version/behavior, web consumers, persisted schemas, or engine callers.

**Before → after:** Explicit prospective-v2 evaluation allows password prose, recognizes inherited credential shapes, and recognizes key-like material only with `purpose="user_ingress"`. Ordinary production calls remain v1.

**Failing-first tests:** Every family; field literal versus prose; purpose matrix; URL/reference placeholders; overlapping-match precedence; safe ordinal positions; Unicode spans; malformed values; bounds; whole-token exclusions; narrowing against frozen v1. Include the maximum-size adversarial corpus and deliberate evaluator work-bound violations.

**Pins:** New catalogue/corpus coverage; P1 remains preserved. The v1 evaluator is not deleted or rewritten to use new semantics.

**Compatibility/operator effect:** No activation. Estimated 450–600 lines.

## Lane CRED-A3

**Purpose:** Generate a TypeScript catalogue and prove browser/server behavioral parity.

```json
{
  "ticket": "CRED-A3",
  "title": "Generate browser credential catalogue and prove runtime parity",
  "files": [
    "scripts/generate_credential_formats.py",
    "src/elspeth/web/frontend/src/generated/credentialFormats.ts",
    "src/elspeth/web/frontend/src/lib/credentialMaterial.ts"
  ],
  "test_files": ["tests/unit/scripts/test_generate_credential_formats.py"],
  "test_command": "pytest tests/unit/scripts/test_generate_credential_formats.py -n 0 -q",
  "branch": "lane/CRED-A3",
  "worktree": "<repo>/.claude/worktrees/lane-cred-a3"
}
```

**Edits:** Manifest, primary test, `credentialMaterial.test.ts` beside the evaluator, and A2’s recipe corpus where parity cases are needed.

**Must not edit:** Components, store, API client, `tsconfig`, server activation, or the authoritative rules independently.

**Before → after:** No browser guard yet; a deterministic generated module and tested evaluator become available.

Emit a `.ts` module containing JSON-literal data and a catalogue digest. This avoids introducing `resolveJsonModule`, which the current app configuration does not set. Generate only the browser-supported policy records, while Python retains historical v1.

**Failing-first tests:** Regeneration equals committed bytes; Python and actual JavaScript execution produce identical finding records; UTF-16 conversion; maximum-size adversarial inputs. Mutation controls change a rule, a constraint, and one generated byte and must fail. Recipe inputs avoid committing literal credentials.

**Pins:** Generated artifact and shared corpus are new pins. The Python ticket command checks generation; the frontend test command must also run in Node. A Python-only result is not parity evidence.

**Compatibility/operator effect:** No user-visible activation. Estimated 450–600 lines, including generated output.

## Lane CRED-B1

**Purpose:** Remove accidental coupling between admission, reference placement, fingerprints, catalogue projection, and ECS screening.

```json
{
  "ticket": "CRED-B1",
  "title": "Separate credential field roles and inventory every plugin config field",
  "files": [
    "src/elspeth/contracts/credential_fields.py",
    "src/elspeth/core/secrets.py",
    "src/elspeth/core/config.py",
    "src/elspeth/web/catalog/service.py",
    "src/elspeth/web/catalog/schema_parse.py",
    "src/elspeth/web/composer/provider_context.py",
    "src/elspeth/web/_aws_ecs_acceptance/task_definition.py",
    "scripts/cicd/inventory_credential_fields.py"
  ],
  "test_files": ["tests/unit/contracts/test_credential_field_roles.py"],
  "test_command": "pytest tests/unit/contracts/test_credential_field_roles.py -n 0 -q",
  "branch": "lane/CRED-B1",
  "worktree": "<repo>/.claude/worktrees/lane-cred-b1"
}
```

**Edits:** Manifest and primary test. The script emits the live-registry before/after table; its output is implementation evidence, not a manually maintained source-field count.

**Must not edit:** Plugin field declarations, database schema, stored fingerprints, reference authorization, detector activation, or deployment configuration.

Current coupling is verified at `core/secrets.py:24–27,299`, `core/config.py:2801,2842`, `catalog/service.py:525`, `schema_parse.py:80,93`, `provider_context.py:109`, and `task_definition.py:131`.

**Before → after:** Role-specific helpers initially preserve active-v1 behavior. Prospective-v2 classification stops treating ordinary `_key`/`_token` fields as credentials, while AWS session credentials remain covered. Reference placement and existing config fingerprint bytes remain stable.

**Failing-first tests:** Every live registered field appears exactly once per schema path/variant; nested/union schemas are covered; `aws_session_token` positive control; ordinary structural-key negative control; existing secret-reference placement remains accepted; config fingerprints remain identical across the refactor. Verify ECS screening retains its conservative cases.

**Pins:** New inventory coverage, existing fingerprint tests, P8. Inventory derives from the live manager at `plugins/infrastructure/manager.py:177–185,334–360`.

**Compatibility/operator effect:** No re-fingerprinting or migration. Estimated 450–600 lines. Full inventory execution belongs to implementation, not this read-only design turn.

## Lane CRED-B2

**Purpose:** Atomically activate v2 narrowing and evidence compatibility.

```json
{
  "ticket": "CRED-B2",
  "title": "Activate narrowing-only detector v2 with compatible evidence readers",
  "files": [
    "src/elspeth/contracts/credential_material.py",
    "src/elspeth/contracts/plugin_assistance.py",
    "src/elspeth/web/credential_guard.py",
    "src/elspeth/web/validation.py",
    "src/elspeth/web/composer/diagnostic_evidence.py"
  ],
  "test_files": ["tests/unit/web/test_credential_label_prose.py"],
  "test_command": "pytest tests/unit/web/test_credential_label_prose.py tests/unit/web/test_credential_guard.py tests/unit/web/composer/test_diagnostic_evidence.py tests/unit/contracts/test_credential_material.py -n 0 -q",
  "branch": "lane/CRED-B2",
  "worktree": "<repo>/.claude/worktrees/lane-cred-b2"
}
```

**Edits:** Manifest and primary tests, plus affected expectations in `test_plugin_assistance.py`, `test_routes_state.py`, and the B2-owned portions of `test_visible_tool_event_persistence.py`.

**Must not edit:** User-ingress purpose selection, browser, repair calls, fingerprints, sentinel bytes, or native-thinking policy.

Change `CREDENTIAL_DETECTOR_VERSION` to `"2"` in the same commit that accepts both versions in evidence. Historical metadata rescans explicitly use the record’s version. The current conflict is concrete: the default at `diagnostic_evidence.py:181` follows the constant, while `:188` accepts only `"1"` and `:192` rescans with the current detector.

Remove duplicate credential prose rules from `plugin_assistance.py:31–43`, preserving its independent URL/header/file/exception restrictions. Shared validation helpers default to control semantics; B3 selects ingress only at user-owned boundaries.

**Before → after:** “password: use a secret reference” passes model output and later replay. A known-format credential still fails. Newly key-like strings do not start failing engine evidence.

**Failing-first tests:** Model output followed by replay; saved state, MCP state, provider-request history, and sink-effect checks; old envelope byte/hash preservation; v1 metadata accepted/rejected under v1; v2 writer/read round trip; unknown version and non-string version rejection; no newly rejected control cases in the shared corpus.

**Pins:** P1–P4, P6, P8. Do not relax integrity checks or rewrite stored envelopes.

**Compatibility/operator effect:** New records carry detector version 2; v1 remains readable. No stored-data scan or migration is required to activate narrowing. Estimated 450–600 lines.

## Lane CRED-B3

**Purpose:** Activate the wider policy exclusively on new user-authored input.

```json
{
  "ticket": "CRED-B3",
  "title": "Apply explicit wider credential policy at user input boundaries",
  "files": [
    "src/elspeth/web/credential_guard.py",
    "src/elspeth/web/validation.py",
    "src/elspeth/web/sessions/schemas.py",
    "src/elspeth/web/sessions/routes/composer/operations.py",
    "src/elspeth/web/sessions/routes/composer/state.py",
    "src/elspeth/web/sessions/routes/sessions.py",
    "src/elspeth/web/sessions/routes/workflow/approvals.py",
    "src/elspeth/web/sessions/routes/workflow/reviews.py",
    "src/elspeth/web/sessions/routes/workflow/library.py"
  ],
  "test_files": ["tests/unit/web/test_credential_ingress_purposes.py"],
  "test_command": "pytest tests/unit/web/test_credential_ingress_purposes.py tests/unit/web/test_credential_boundary_inventory.py -n 0 -q",
  "branch": "lane/CRED-B3",
  "worktree": "<repo>/.claude/worktrees/lane-cred-b3"
}
```

**Edits:** Manifest, primary test, purpose-aware boundary inventory, and affected route tests.

**Must not edit:** Provider/replay policy, library publication of existing state, recompose semantics, secret-entry routes, blob contents, or engine callers.

Verified boundaries include operation admission at `operations.py:137–148`, titles/fork at `sessions.py:695,809,891`, amendments at `schemas.py:646`, workflow note validators, and imported state at `composer/state.py:1282`, before subsequent effects.

Shared amendment/tool helpers must receive an explicit purpose. Do not make `_validate_accepted_value_content` globally ingress-aware: model tools also use it.

For YAML, apply ingress to user-authored control content before persistence/materialization, respecting exact secret references and the existing data-plane projection. Do not scan server-generated state metadata as if the user authored it.

**Before → after:** The same key-like string is refused in a new title, note, chat message, fork edit, amendment, or YAML control value, but is not newly refused when already present in stored history or engine evidence.

**Failing-first tests:** Each boundary; rejection before operation/body/blob/state writes and provider calls; direct HTTP bypass of browser; a stored version of the same value remains replayable; secrets-panel success; library/recompose negative controls. Mutating one purpose back to `control` must fail the inventory/behavior tests.

**Pins:** P3 and P6. Estimated 350–550 lines. Release notes must identify broader checks on new user submissions without claiming a historical cleanup.

## Lane CRED-C1

**Purpose:** Correct copy, structured transport, model guidance, and MCP error classification without adding provider calls.

```json
{
  "ticket": "CRED-C1",
  "title": "Expose safe credential reasons and correct refusal classifications",
  "files": [
    "src/elspeth/web/app.py",
    "src/elspeth/web/credential_guard.py",
    "src/elspeth/web/validation.py",
    "src/elspeth/web/sessions/composer_turn.py",
    "src/elspeth/web/sessions/routes/_helpers.py",
    "src/elspeth/web/composer/tool_batch.py",
    "src/elspeth/web/composer/error_codes.py",
    "src/elspeth/web/composer/tools/generation.py",
    "src/elspeth/web/composer/tools/_dispatch.py",
    "src/elspeth/composer_mcp/server.py",
    "src/elspeth/web/composer/skills/pipeline_composer.md"
  ],
  "test_files": ["tests/unit/web/composer/test_credential_feedback.py"],
  "test_command": "pytest tests/unit/web/composer/test_credential_feedback.py tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_error_code_redaction.py -n 0 -q",
  "branch": "lane/CRED-C1",
  "worktree": "<repo>/.claude/worktrees/lane-cred-c1"
}
```

**Edits:** Manifest; primary test; affected route-error, MCP-server, guard-copy, and skill-loader tests.

**Must not edit:** Provider retry/accounting, detector rules, native thinking, historical guidance regex fixture, progress-reason enum, or graph-authoring logic.

The global Pydantic handler only allows `type/loc/msg` at `app.py:2665`; simply adding `ctx` to an exception will not reach the browser. Extract only the owned credential context schema for recognized credential errors. Continue dropping arbitrary `ctx`, `input`, and exception objects.

Correct all three copy paths:

- `composer_turn.py:1832–1844`.
- `sessions/routes/_helpers.py:929–942`.
- Baseline refusal at `tool_batch.py:3296–3312`.

Keep existing progress-reason plumbing; use the credential-specific structured failure code and accurate text. No new `composer_progress.py` literal is needed.

At `_dispatch.py:912`, convert a credential refusal into a safe normalized failure where a valid admitted tool call exists. At MCP `server.py:884–899`, catch credential refusal before generic plugin-crash handling and return/audit `ARG_ERROR` consistently.

**Failing-first tests:** User/model custody-specific copy; sanitized Pydantic transport; no arbitrary key/value leakage; all three wrong-copy paths; catalogue lookup; captured tool response; MCP result/audit parity; genuine runtime failure still classified as plugin crash. Provider call count must remain unchanged.

**Pins:** P2, P3, P5, P6, P9. Skill guidance says to use references and advise rotation; it does not imply the model receives blocked user input.

**Compatibility/operator effect:** Additive safe fields and corrected error classification. Estimated 450–600 lines.

## Lane CRED-C4

**Purpose:** Make credential disclosures durable and correctly attributed, including after successful repair.

```json
{
  "ticket": "CRED-C4",
  "title": "Persist credential disclosures through canonical trusted notices",
  "files": [
    "src/elspeth/web/composer/protocol.py",
    "src/elspeth/web/composer/no_tool_policy.py",
    "src/elspeth/web/composer/service.py"
  ],
  "test_files": ["tests/unit/web/composer/test_credential_notices.py"],
  "test_command": "pytest tests/unit/web/composer/test_credential_notices.py tests/unit/web/composer/test_no_tool_policy_segments.py -n 0 -q",
  "branch": "lane/CRED-C4",
  "worktree": "<repo>/.claude/worktrees/lane-cred-c4"
}
```

**Edits:** Manifest, primary test, notice round-trip tests, and route persistence tests exercising existing receipt writers.

**Must not edit:** Database schema, session persistence policy, frontend segment types, provider retry, or native-thinking custody.

Add an owned, default-empty tuple of safe disclosures to `ComposerResult`. Render through the canonical trusted suffix grammar and the common finalization path. C2/C3 populate it; finalizers preserve it.

**Before → after:** A repaired answer no longer silently conceals that an earlier model attempt was withheld. The notice survives reload and remains `trusted_system_notice`.

**Failing-first tests:** Successful repair disclosure; coexistence with preflight/handoff notices; exactly one suffix; safe raw-content attribution; same-turn persisted-assistant identity; reload; spoofed user/model marker text remains untrusted; omitted notice propagation fails.

**Pins:** P10 and `protocol.py:452,462,528`; preserve the prefix and same-turn contracts. Register every new template/parser arm and its round-trip case.

**Compatibility/operator effect:** No storage migration; newly written notices use the existing message/receipt mechanism. Estimated 350–550 lines.

## Lane CRED-C2

**Purpose:** Add a bounded compose repair attempt after credential-bearing model output, with complete accounting.

```json
{
  "ticket": "CRED-C2",
  "title": "Repair refused compose output once with safe feedback and full accounting",
  "files": [
    "src/elspeth/web/composer/credential_repair.py",
    "src/elspeth/web/composer/service.py",
    "src/elspeth/web/composer/_compose_loop_carriers.py"
  ],
  "test_files": ["tests/unit/web/composer/test_credential_compose_repair.py"],
  "test_command": "pytest tests/unit/web/composer/test_credential_compose_repair.py tests/unit/web/composer/test_compose_loop_llm_audit.py tests/unit/web/composer/test_compose_loop_tool_call_cap.py -n 0 -q",
  "branch": "lane/CRED-C2",
  "worktree": "<repo>/.claude/worktrees/lane-cred-c2"
}
```

**Edits:** Manifest, primary test, and relevant context-budget/persistence accounting tests.

**Must not edit:** Gateway’s generic retry policy, CDA-18 reasoning rules, detector semantics, audit schemas, or tutorial branches.

Handle repair above the ordinary per-call transport retry. Build it from the last admitted history plus a safe credential feedback record. A rejected tool batch is not appended: current guard-before-append ordering is at `tool_batch.py:1132–1172`.

The operation-scoped allowance is consumed once, including when planner repair participates. A retry never resets the original deadline or quota reservation.

**Before → after:** One recoverable credential-bearing completion may produce a safe second completion with a durable notice. Repeated failure, insufficient budget, expired deadline, or invalid native-history custody ends explicitly.

**Failing-first tests:** Actual outgoing repair messages; no rejected bytes, tool IDs, signatures, or arguments; charged/audited first and second physical calls; cancellation; timeout; call cap; quota denial; context shedding; second refusal; mixed tool batch atomicity; successful notice propagation. Assert provider calls per transition, including rootless and tutorial entry through the common path.

**Pins:** P6, P10; CDA-18 accounting/custody tests remain authoritative. Estimated 450–600 lines. This lane receives a separate provider-call/accounting review.

## Lane CRED-C3

**Purpose:** Carry safe credential reasons through planner feedback and its bounded repair path.

```json
{
  "ticket": "CRED-C3",
  "title": "Integrate credential repair and disclosures into planner feedback",
  "files": [
    "src/elspeth/web/composer/pipeline_planner.py",
    "src/elspeth/web/composer/planning_application.py"
  ],
  "test_files": ["tests/unit/web/composer/test_credential_planner_repair.py"],
  "test_command": "pytest tests/unit/web/composer/test_credential_planner_repair.py tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py -n 0 -q",
  "branch": "lane/CRED-C3",
  "worktree": "<repo>/.claude/worktrees/lane-cred-c3"
}
```

**Edits:** Manifest, primary test, and affected planner accounting/context tests.

**Must not edit:** Candidate construction rules, graph synthesis, generic context-budget ranking, active detector purposes, or independent retry allowances.

Use the direct guidance path at `pipeline_planner.py:2441–2465,2542`. Integrate the current credential catch paths at `:4342–4373` and `:4471–4483` with C2’s shared allowance. Preserve the first refused call’s safe audit record.

Add a safe notice tuple to the owned planner result and propagate it through `planning_application.py` into C4’s final result channel.

**Before → after:** The planner receives an actionable safe reason instead of only aborting. It must author any replacement graph itself.

**Failing-first tests:** Captured planner repair request; catalogue explanation and fix; one shared compose/planner allowance; failed attempt accounted once; no refused candidate persisted; second refusal; no feedback shedding; resulting user notice.

**Pins:** P5, P6, P9, P10. Measure actual scaffold changes; do not assume a skill-text edit moves the planner harness. Estimated 350–550 lines.

## Lane CRED-D

**Purpose:** Stop credential-bearing browser submissions before optimistic state, custody storage, or HTTP dispatch.

```json
{
  "ticket": "CRED-D",
  "title": "Guard browser send and fork custody plus chat paste and drop",
  "files": [
    "src/elspeth/web/frontend/src/stores/sessionStore.ts",
    "src/elspeth/web/frontend/src/components/chat/ChatInput.tsx",
    "src/elspeth/web/frontend/src/components/chat/MessageBubble.tsx"
  ],
  "test_files": ["tests/unit/web/test_credential_boundary_inventory.py"],
  "test_command": "pytest tests/unit/web/test_credential_boundary_inventory.py -n 0 -q",
  "branch": "lane/CRED-D",
  "worktree": "<repo>/.claude/worktrees/lane-cred-d"
}
```

**Edits:** Manifest, the boundary inventory, and colocated frontend tests for the store/components.

**Must not edit:** `api/client.ts`, SecretsPanel, secret routes, tutorial-specific submission behavior, or the generated catalogue by hand.

Primary guards belong at `sessionStore.sendMessage:1279` and `forkFromMessage:1771`, before custody/optimistic mutation. Component guards cover paste/drop and preserve drafts. Current `ChatInput` clears immediately after calling `onSend` at `:304–315`; rejection must not clear it or close a fork editor.

This covers store callers in ChatPanel, `SideRailValidationBanner.tsx:41`, and `TutorialFreeformShell.tsx:149`, plus retry through `sessionStore.ts:1759`. Recovery of an already submitted durable operation is not reclassified as new input or silently discarded.

**Failing-first tests:** Paste, drop, keyboard send, programmatic send, retry, fork; no HTTP call, operation ID, sessionStorage write, optimistic message, or draft loss on rejection; secrets-panel submission still works; safe prose and references work.

Add a placement control that fails if the guard is moved to `api/client.ts`. Pair the Python boundary inventory with actual frontend behavior tests; a source-shape assertion alone does not prove protection.

**Pins:** P6 and A3’s generated parity contract. Estimated 400–600 lines. Node/frontend tests are mandatory in addition to the ticket’s pytest command.

## Lane CRED-E1

**Purpose:** Produce bounded, complete, explained Composer egress projections.

```json
{
  "ticket": "CRED-E1",
  "title": "Make Composer egress redaction bounded complete and explained",
  "files": [
    "src/elspeth/web/validation.py",
    "src/elspeth/web/composer/advisor_context.py",
    "src/elspeth/web/composer/advisor_abandonment.py",
    "src/elspeth/web/composer/turn_effects.py",
    "src/elspeth/web/sessions/_auto_title.py"
  ],
  "test_files": ["tests/unit/web/composer/test_credential_egress.py"],
  "test_command": "pytest tests/unit/web/composer/test_credential_egress.py -n 0 -q",
  "branch": "lane/CRED-E1",
  "worktree": "<repo>/.claude/worktrees/lane-cred-e1"
}
```

**Edits:** Manifest, primary test, relevant advisor/title tests, and the shared adversarial corpus if egress cases require additions.

**Must not edit:** Control admission, ingress tiers, audit sentinel bytes, engine target identities, or advisor capability/authority decisions.

Replace sequential string substitutions at `validation.py:179–191` with a projection result containing redacted content and safe reasons. Preserve a compatibility string-returning wrapper for callers needing only content.

For structured values, redact the entire credential-field scalar. For ambiguous credential-labelled free text, withhold the complete scalar rather than leaving a multiword secret’s tail. PII remains separately identified; do not call an email match a credential.

Advisor abandonment currently projects content and parts separately at `advisor_abandonment.py:66–82`. Derive both from one projection so trust segments and displayed text cannot diverge. Bind review/cache identity to the final projected content at `turn_effects.py:818,856,868`, while preserving separate authorization identities.

Title context currently redacts then truncates at `_auto_title.py:178–179`. Keep the disclosure outside the truncated, untrusted excerpt.

**Failing-first tests:** Multiword assignment tail removed; prose admitted but conservatively redacted only on egress; reason adjacent to model projection; content/parts agreement; projection identity agreement; title truncation retains notice; long email/JWT corpus; safe negatives; no key-like widening.

**Pins:** P7 unchanged; P11 only if declared projection manifests actually change. Estimated 450–600 lines.

## Lane CRED-E2

**Purpose:** Preserve valid structured evidence and attach safe reasons without changing `<redacted-secret>`.

```json
{
  "ticket": "CRED-E2",
  "title": "Preserve structured credential audit projections and companion reasons",
  "files": [
    "src/elspeth/web/composer/diagnostic_evidence.py",
    "src/elspeth/web/composer/visible_tool_event.py"
  ],
  "test_files": ["tests/unit/web/composer/test_visible_tool_event_persistence.py"],
  "test_command": "pytest tests/unit/web/composer/test_visible_tool_event_persistence.py tests/unit/web/composer/test_diagnostic_evidence.py -n 0 -q",
  "branch": "lane/CRED-E2",
  "worktree": "<repo>/.claude/worktrees/lane-cred-e2"
}
```

**Edits:** Manifest and the two named test files, strictly within E2’s assertion ownership.

**Must not edit:** `REDACTED_CREDENTIAL_TEXT`, engine scrub wrappers, sink-effect identity maps, historical evidence bytes/hashes, or the judge scrubber.

`VisibleToolEvent.capture` currently redacts serialized JSON as text at `visible_tool_event.py:123`, then tries to parse it again at `:125–128`. Instead, bounded-decode structured slots, remove inline custody data, project credential values, then serialize. Preserve explicit unavailable gaps for genuinely malformed/duplicate-key/over-limit input.

Use existing companion channels:

- `DiagnosticPayload.reasons` receives closed, catalogue-derived reason identifiers for new v2 records.
- Visible evidence’s `availability_gaps` can record the specific credential content that is unavailable, even when safe sibling fields remain available.
- The sentinel remains at the redacted position. No reason text is inserted into hashed engine payload maps.

Historical reasons remain readable under their existing interpretation. Historical generic sentinels are displayed as “credential material redacted; format unavailable,” not assigned a fabricated family.

**Before → after:** A valid structured capture containing a credential retains safe siblings as valid JSON, instead of losing the whole slot because regex substitution damaged JSON.

**Failing-first tests:** Safe sibling retained; full credential removed; inline blob canary absent; duplicate-key refusal; reason/source agreement; v1 envelope round trip unchanged; v2 reasons accepted only from the closed catalogue; mutated payload/hash rejected. Replace the invalid-JSON expectation at `test_visible_tool_event_persistence.py:357–384`.

**Pins:** P4, P7, P11. The original sentinel-changing E2 is removed from scope; this smaller E2 owns projection integrity and explanations. Estimated 350–550 lines.

## Lane CRED-F

**Purpose:** Make repository scanning use the shared detector without printing credential values.

```json
{
  "ticket": "CRED-F",
  "title": "Use shared credential rules in staged scanning and required CI",
  "files": [
    "scripts/scan_credentials.py",
    "scripts/git-hooks/pre-commit-secret-scan.sh",
    ".github/workflows/ci.yaml"
  ],
  "test_files": ["tests/unit/scripts/test_pre_commit_secret_scan.py"],
  "test_command": "pytest tests/unit/scripts/test_pre_commit_secret_scan.py tests/unit/scripts/test_credential_scan_ci.py -n 0 -q",
  "branch": "lane/CRED-F",
  "worktree": "<repo>/.claude/worktrees/lane-cred-f"
}
```

**Edits:** Manifest and named tests.

**Must not edit:** Product false-positive policy, runtime admission, CI aggregate dependencies, or judge signing.

The hook reads staged blobs, not worktree files. It reports path, line/position, rule, and remediation, never the matched line. Use the catalogue’s repository-scan selection explicitly; do not activate all ingress heuristics implicitly on source code.

The current hook has its own pattern list at `:31–43`, passes a pattern to `git grep` without `-e` at `:75`, masks failures with `|| true`, and prints matches at `:85–87`. Replace that path with the shared CLI.

Preserve existing line-level `secret-scan: allow-this-line` handling for repository fixtures. This is not a product false-positive escape. Unreadable/oversized blobs produce an explicit nonzero incomplete-scan result.

Add the same command to the existing required static-analysis job at `ci.yaml:58`, rather than creating a new job and aggregate dependency.

**Failing-first tests:** Staged secret/clean working copy; clean stage/dirty secret; leading-dash PEM marker; filename edge cases; catalogue parity; no value in stdout/stderr; scanner failure cannot pass; explicit fixture annotation.

**Pins:** P12. Estimated 300–500 lines. Patch adds developer tooling only.

## Lane CRED-G

**Purpose:** Report the impact of any proposed future widening across stored Composer and engine data, without altering either.

```json
{
  "ticket": "CRED-G",
  "title": "Report stored credential policy impact without changing data",
  "files": [
    "scripts/credential_detector_impact.py"
  ],
  "test_files": ["tests/unit/scripts/test_credential_detector_impact.py"],
  "test_command": "pytest tests/unit/scripts/test_credential_detector_impact.py -n 0 -q",
  "branch": "lane/CRED-G",
  "worktree": "<repo>/.claude/worktrees/lane-cred-g"
}
```

**Edits:** Script, primary test, and `tests/testcontainer/web/test_credential_detector_impact.py`.

**Must not edit:** Stored sessions, findings, hashes, migrations, detector activation, engine validators, or database schema.

The report covers persisted content consumed by session replay/state/MCP scans **and engine evidence**, including sink-effect target validation. A session-only report cannot justify engine widening: `contracts/sink_effects.py:289` scans evidence and `sink_effect_identity.py:116–125` refuses scrub-changing target maps.

Use bounded pagination and read-only database access. Report owned record identifiers, surface, old/new decisions, rule IDs, and safe positions. Do not print content, credential fragments, or value hashes. Separate newly refused, newly accepted, unchanged, and uninspectable records.

Historical evidence-reader validation and proposed current policy evaluation are separate columns; never reinterpret a v1 record as if it were authored under v2.

**Failing-first tests:** Session and engine positives; unchanged controls; missing-table/source coverage; interrupted/incomplete scan; read-only enforcement; unchanged record bytes/hashes; SQLite and PostgreSQL behavior. Report counts derive from actual rows processed and reconciled queries.

**Pins:** New source-coverage manifest with positive and omission controls. Estimated 400–600 lines.

**Compatibility/operator effect:** Reporting only. Future widening outside ingress requires a separately approved lane and detector version, with an explicit preserve/refuse decision. This lane does not authorize migration, deletion, or activation.

## Verified pins and required ownership

These are concrete pins inspected for this design, not a claim that unrun whole-tree checks will find no others.

| Pin | Verified anchor and required treatment |
|---|---|
| **P1 — detector corpus** | `tests/unit/contracts/test_credential_material.py:42–64` covers registered patterns; `:67` pins broad suffix behavior; `:191–210` covers serialized assignment/newline behavior. A1 preserves legacy coverage; B2 changes active-v2 expectations with positive controls. |
| **P2 — refusal envelope/copy** | `tests/unit/web/test_credential_guard.py:309–325,330–353,377–383` pins exact payloads, version and user/model copy. B2 owns version/structure; C1 owns wording/transport. |
| **P3 — YAML refusal** | `tests/unit/web/sessions/test_routes_state.py:2804–2818,2907–2921` pins exact refusal payloads and forbids field/value leakage. Preserve those negative assertions. |
| **P4 — persisted evidence** | `diagnostic_evidence.py:94,153,181–192,235,239` enforces keys, version, metadata validation and hashes. `test_diagnostic_evidence.py:327` rejects wrong primitive types; it is not a version-1-only compatibility test. `test_visible_tool_event_persistence.py:357–384` pins the JSON-redaction gap. B2 and E2 ownership is split above. |
| **P5 — closed codes/guidance** | `error_codes.py:80`; `test_error_code_redaction.py:365–386`; `test_validation_guidance_catalogue.py:88–142`. Add direct records. Preserve `validation_guidance_legacy_patterns.json`. `test_generation_response_contracts.py:113–114` pins producer bytes against its frozen fixture; regenerate only for an intentional changed producer, never merely to silence a failure. |
| **P6 — boundary inventory** | `test_credential_boundary_inventory.py:324–360,572–604` covers provider owners, common tool owners, state guards and materialization. Extend for explicit purposes and browser ownership, with mutation controls. |
| **P7 — sentinel consumers** | `credential_material.py:23`, `secret_scrub.py:16`, `engine/executors/sink.py:1092–1106`, `contracts/errors.py:26,85`, `private_diagnostic_facts.py:265–268`. Preserve exact `<redacted-secret>` bytes and existing equality behavior; no mass sentinel-fixture rewrite. |
| **P8 — whole-tree contracts** | `CONTRIBUTING.md` whole-tree gates; `config/cicd/soft-mapping-census.yaml`; `scripts/check_contracts.py:1692–1700,1752–1759`; `test_sessions_composer_attribute_contracts.py:100–107`. Integrator owns final census regeneration. No new dynamic probes, suppressions, or hand-edited signatures. |
| **P9 — prompt/hash pins** | `skills/__init__.py:40–65` computes and verifies the actual skill hash. `test_pipeline_planner.py:310–314` excludes the interaction skill from that harness; `:333–347,1722–1737` pins its scaffold budget. Measure actual changes and retain the over-budget negative control. Preserve the generated tool-inventory block governed by `generate_skill_inventory.py:317–342`. |
| **P10 — trusted suffix grammar** | `CONTRIBUTING.md:320–357`; `test_no_tool_policy_segments.py:366–397`; `service.py:487–520`; `protocol.py:452,462,528`. C4 owns new templates, splitter registration, round-trip coverage and single-suffix composition. |
| **P11 — redaction manifest** | `test_adequacy_guard.py:611–637` compares `redaction_policy_snapshot.json` with live `MANIFEST`. Regenerate through `scripts/cicd/bootstrap_redaction_snapshot.py --write` only if declared projection policy changes. |
| **P12 — staged scanner** | `test_pre_commit_secret_scan.py:17–27` currently packages only the shell script; `:36–53` includes a token-printing assertion that must be reversed; `:56–71` is the unstaged-secret negative control. |

A lane’s edit allowlist is its JSON `files`, named test files, and explicitly listed fixtures. Everything else is excluded. Conditional generated-pin changes belong to the integrator after the responsible production change is understood. If a lane cannot fit approximately 600 changed lines including tests/generated output, split its implementation before dispatch; do not omit required tests or discount generated lines.

## Compatibility, limitations, and verification

The active detector changes once, in B2. Historical v1 patterns remain permanently selectable, and historical evidence retains its original bytes, reasons, version, and hashes. Narrowing admission does not authorize rewriting old records or discarding integrity failures.

The review is correct that new key-like/vendor rules cannot be enabled globally. They apply only to new user ingress in this design. No new engine failure is accepted as a side effect of improved chat protection.

The report’s statement that stored findings are value-free does not establish that all surrounding persistence is hash-free: diagnostic envelopes and retained payloads have integrity hashes, and provider error handling has separate raw-error hashing. Those contracts must be preserved. Likewise, the legacy guidance fixture exists, and the current guard already has model-output-specific copy at `credential_guard.py:38–43`; the remaining defect is that some callers replace it with access/quota messaging.

The judge scrubber, **`elspeth-lints/src/elspeth_lints/.../source_excerpt.py`, is deliberately unchanged**. This work also does not scan uploaded blob contents or claim to be comprehensive DLP.

Implementation verification must include:

- Failing-first tests and the ticket commands, with explicit process exit codes.
- Actual Node/Vitest execution for A3/D, including browser/server corpus parity and maximum-size inputs.
- Focused persistence tests plus PostgreSQL testcontainer coverage for changed evidence/session persistence behavior.
- Serial timing-sensitive checks for the new bounded evaluators and provider-repair deadlines.
- Ruff, relevant mypy/contracts checks, whole-tree boundary/attribute/template/code-catalogue gates, and before/after keyless trust-tier findings.
- One integrator-owned full-suite gate for the integrated shared-runtime change, with both worktree source roots on `PYTHONPATH`, frozen-tree provenance, logs, and measured host capacity.

PostgreSQL/container checks and browser/integration tooling requiring unavailable sockets must run outside a socket-less sandbox. Trusted operator signature verification remains separate; agents do not acquire the signing key.

Operator-facing release notes should state: accidental-input protection is broader on new user submissions; password prose and structural field names produce fewer false refusals; blocked material receives safe explanations; one bounded repair may add a provider call; browser rejection precedes tab custody storage; historical evidence remains readable; no records are migrated or deleted.
