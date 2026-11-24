# Decisions made by matching free text in the Web Composer (2026-11-15)

Read-only survey by a systems-thinking agent, at the maintainer's request, of
`src/elspeth/web/`, the composer MCP surface, the contracts the composer
consumes and the frontend. "Run" means the agent executed the code on an
input; "by reading" means it did not. No suites were run. The agent's search
was calibrated on a known positive (`find_credential_material("password:
hunter2")` returns `token_shape`) and a control (`in str(exc)` finds
`sessions/service.py:5558`).

## Headline

No live code classifies the model's prose by its wording (refusal, apology,
completion claim, tool JSON in prose). Provider error handling is typed. What
remains falls into five groups: drifted copies of the credential detector and
name-suffix rules; ELSPETH's own messages re-parsed into codes; a few keyword
rules over user or model text; one durable flag stored as a suffix of a prose
column; and frontend display guessers.

## Ranked instances

Severity: H is a security or integrity control that can leak or
mis-attribute; M is a correctness gate that can block a user or misdirect the
planner; L is cosmetic.

| # | Site | What is matched and what depends on it | Sev | Evidence |
|---|---|---|---|---|
| 1 | `web/composer/llm_response_parsing.py:98-116, 643-672` | Provider-error scrubber: a drifted second copy of the credential patterns; decides what is stored in `error_message` on audit rows | H | run |
| 2 | `contracts/credential_material.py:91-103`, `83-90`, `149-160` | `credential_assignment`, and the rule that any key ending `_key`, `_token`, `_secret`, `_password` or `_credential` is a credential field | M (H as a control) | run |
| 3 | `web/validation.py:105-135, 166-199` | Egress redactor: phone, SSN and email shapes | M | run |
| 4 | `web/composer/implicit_decisions.py:458-475` | Provenance label in the auditor report decided by field-name suffix and value | H (audit) | by reading |
| 5 | `web/sessions/pending_interpretation.py:622-626, 768-775, 976-990` | "Structural directive" guard on prompt text | M | run |
| 6 | `web/composer/protocol.py:1176-1262` | `ToolArgumentError` canonicalises caller prose by substring (70 raise sites) | M | run |
| 7 | `web/composer/state.py:1940-1954, 2098-2120` | Plugin draft-config tolerance keyed on a `ValueError` message prefix | M | by reading |
| 8 | `sessions/protocol.py:165-167`, `sessions/service.py:8322`, `coordination/run_recovery_authority.py:281-283`, `web/app.py:471` | Three-state Landscape reconciliation flag stored as a suffix of the free-text `runs.error` column | M | by reading |
| 9 | `web/composer/no_tool_policy.py:25-35, 540-820` | Trusted notices appended to the model's reply and re-recognised by byte shape (about 21 shapes) | M | by reading |
| 10 | `web/composer/tools/generation.py:458-1900, 1960-1975, 2189-2260` | `explain_validation_error`: 129 regexes over ELSPETH validator prose, then a substring scan of 170 codes | L-M | run |
| 11 | `web/interpretation_state.py:194-195`; `tools/transforms.py:941, 1470, 2013`; `tools/sessions.py:2084-2086` | `error_code` derived by `startswith` on ELSPETH's own message | L-M | run |
| 12 | `composer/yaml_generator.py:510`, `tools/blobs.py:1438`, `sessions/routes/sessions.py:258` | Option keys ending `_blob_id` treated as blob references | L-M | by reading |
| 13 | `composer/audit.py:1461-1465`, `protocol.py:1011-1015` | Pydantic error `type` classified by substring, plus an exact message compare | L | by reading |
| 14 | `composer/advisor_output.py:185-262` | Model-authored advisor note: URL and email regex, and a stripped in-band fence sentinel | L-M | by reading |
| 15 | `llm_response_parsing.py:886`, `provider_config.py:35`, `reasoning.py:56`, `advisor_request.py:59` | Model-id prefix or a `claude-` substring picks cache markers and dialect | L-M | by reading |
| 16 | frontend `SecretsPanel.tsx:96-115` | Substrings of the backend 422 detail choose the `aria-invalid` field | L-M | run |
| 17 | frontend `ComposingIndicator.tsx:108`, `validationHumaniser.ts:290-420`, `ExecuteButton.tsx:94`, `approvalRows.ts:12` | Keyword and id-token guessing for display | L | by reading |
| 18 | `wire_projection.py:260`, `titles.py:44`, `source_inspection.py:371`, `tutorial_service.py:598` | Omission-phrase lint, default-title regex, file kind by name, artifact format by suffix | L | by reading |

## The six that matter most

1. **Provider-error scrubber (#1).** Missed: `401 Incorrect API key provided:
   sk-proj-…` is stored verbatim, because the local pattern is
   `sk-[A-Za-z0-9]{40,}` with no hyphen; the shared detector flags the same
   key. False trigger: `Field customer.address.street is missing` becomes
   `<redacted-sensitive:jwt>`. A key can reach a durable audit row, and
   diagnostics lose ordinary field names. Fix: delegate to
   `CREDENTIAL_PATTERN_SPECS`; about 30 lines.
2. **Credential keyword and suffix rules (#2).** False trigger: "the
   password: must be rotated every 90 days"; "credentials: stored in vault";
   a key named `sort_key`, `join_key`, `dedup_key` or `cache_key` with the
   value `order_id` is refused as a credential field. Missed: "Password is
   hunter2". A legitimate join or sort pipeline is refused. Fix: format
   matching for values; for field names use schema-declared credential fields
   in place of the suffix list. CDA-19 as briefed does not name the suffix
   rule.
3. **Provenance by suffix (#4).** A deliberate `allowed_hosts=public_only` is
   recorded as "default"; a non-LLM node's `temperature` as "picked"; a newly
   defaulted field as "composer_selected". The auditor-facing report
   misattributes who decided. Fix: record provenance where the value is set,
   as a typed field; medium to large.
4. **Directive guard (#5).** "Describe the user's role:", "Classify each row
   by operating system:" and "Use these instructions:" each make resolve raise
   `InterpretationPlaceholderConsumedError` (run). A user who has accepted a
   review is blocked at the run gate by ordinary prose. A dead duplicate list
   sits at `sessions/service.py:463`. Fix: delete the guard, or use the typed
   `prompt_template_parts` and a role-typed `system_prompt`.
5. **Reconciliation flag in `runs.error` (#8).** Error text that already ends
   in a marker, or column truncation, changes queue membership, and
   audit-facing text is edited in place by slicing. Fix: a CHECK-constrained
   column; needs a migration and a compatibility statement.
6. **Trusted notices recognised by shape (#9).** An unregistered new notice
   renders as model prose; a shape change silently demotes trusted chrome.
   Fix: persist typed notice segments beside the content and render from
   them; large.

## Why these keep appearing

An author needs a category from text, and the producer emitted no typed
signal. Three shapes: ELSPETH's own message is re-parsed later (#6 to #11;
`state.py:1940` says "There is no type to match on"); another party authored
the text and a safety call is needed, so patterns are copied per call site
(#1, #2, #3, #14); or a human label is wanted without the typed field to hand
(#4, #17, #18).

The loop that keeps them alive: a false positive is reported, and the
cheapest patch is another entry in a list. Each fixes a symptom and adds an
untested rule. Tests supply the prose the author already had in mind, so the
damage shows on user sessions.

## Removed before

- `9700470e2` (2026-08-13): about 1,200 lines of prose-intent recipe routing,
  replaced by the planner calling tools; the ban became composer invariant 1.
  `e7a85bf8e` (2026-08-26) removed the recipe system.
- `7001600fe` (2026-09-28): the guided composer, including `chat_solver`.
- `752a69bdc` (2026-10-17): `state_claim_grounding.py` (710 lines), replaced
  by typed turn evidence.
- `7067052ba` (2026-10-18): prose-derived threshold rejection.
- `b93d5ce53` (2026-10-14): final replies bound to reviewed effects.
- `8d0bfd281` (2026-05-07): a prose "recovery nudge", reverted.

Each removal followed a fail-open finding. None added a gate.

## Where the leverage is

1. **The producer carries the code.** A typed plugin-config error, `(code,
   message)` returns, a mandatory closed kind on `ToolArgumentError`, a
   reconciliation column, typed notice segments. Start with #7 and #11, which
   are small.
2. **One credential detector with a stated purpose per site** (reject,
   egress, audit scrub), built on `CREDENTIAL_PATTERN_SPECS`. This removes #1
   and #3 and resolves #2.

Proposed guard: a whole-tree test requiring each `re.*` call and each
non-constant `startswith`, `endswith` or `casefold` comparison under `web/`
and the composer MCP surface to carry one of four tags (id shape,
ELSPETH-emitted format, closed credential format, external wire format). It
must be calibrated to flag #2 and #5 and stay quiet on identifier shapes.

## Excluded, with reasons

`provider_errors.py` (typed by SDK exception and closed code; the model to
copy); the producers in `no_tool_policy.py` (ELSPETH literals);
`capability_skill.py:62` (ELSPETH's own table, fails loudly); prompt comment
stripping; the auto-title character allowlist; the registered pipeline
decision terms (closed registry, exact match); JSON-Schema keyword sets; hash,
id, version and schema-name shape checks; the exporter marker parsed at
`routes/composer/state.py:376`; source literal sniffing; the minted `source:`,
`node:`, `output:` and `blob:` id prefixes; self-coded `csv_…` warnings;
closed alias sets; the `web/auth` regexes; tutorial detection (flag-based).

## Not checked

Attacks on #9 and #14; tests beyond a count and four files; the contents of
`skills/*.md`; frontend idioms beyond a search; `contracts/` beyond the
credential detector; `web/execution`, blobs and secrets beyond a search;
`redaction.py` (4,509 lines) was sampled only. The agent also reports that
`validation_guidance_legacy_patterns.json` does not exist in this tree.
