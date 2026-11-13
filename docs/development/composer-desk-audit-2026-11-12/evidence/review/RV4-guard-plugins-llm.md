# RV4-guard-plugins-llm — adversarial review of F4, F6, F11, F14, F15, llm-wiring-verification, R6/R8/R11/R13

Reviewer: RV4. Tree: branch `ccr-d14093ba-glu6bw` @ `45b65e50` (no `src/` diff vs `1a7ff0e6`).
Read-only. Scratch probes are in
`<session scratchpad>`
(`f4_probe.py`, `f11_patch.py`, `a_*.out`, `b.out`, `c_core.out`). Every probe exited 0.

## Verdict table

| Claim | Verdict | One-line reason |
|---|---|---|
| F4: guard fails the turn on credential-shaped text in the model's reasoning | **CONFIRMED-WITH-CORRECTION** | Reproduced in-process: in both turns the reasoning alone trips the guard. Content and every tool argument pass. The code citation is wrong, T2 has a different trigger, the "minimal window" is not minimal, and the scope is wider than the planner. |
| F4 user-facing text | **CONFIRMED** | `terminal.json` body carries `CREDENTIAL_REFUSAL_DETAIL` verbatim (`contracts/credential_material.py:24-26`). |
| F6: default allowlist cannot do arithmetic, type conversion or filtering | **OVERSTATED** | Only computing a new field is blocked. Type conversion at ingest (csv source coercion) and filtering (a gate) both work on the default core. The live model said so itself in c01 T1/T2. |
| F6: `field_mapper` `not_for` names unavailable plugins | **CONFIRMED** | `plugins/transforms/field_mapper.py:338-341`. The "contradictory unless the digest is partial" quote is from smoke-edit T1, not c01. |
| F11: report_assemble → document always shows a HIGH warning that cannot be cleared | **CONFIRMED** | Agent's `b_report_document.py` re-run: same output. Live s08 T1/T2 `state.json` show it (severity high), with `is_valid=true` and the advisor CLEAN. |
| F11: document sink example `schema.mode: fixed` conflicts | **CONFIRMED** | `sink_locked_extras` in the baseline run (`document_sink.py:134-145`). |
| F14: approved vague_term never reaches multi-query prompts | **CONFIRMED-WITH-CORRECTION** | The all-override case reproduces exactly. The title is too broad: in the mixed case the value does reach the fallback queries. No other code path rewrites per-query templates. |
| F15: doc lists 9 always-authorized plugins, compiler has 14 | **CONFIRMED** | `REQUIRED_WEB_PLUGIN_IDS` has 14 (EXECUTED). The doc list is at `environment-variables.md:589-592`. |
| F15: "digest names 8 unavailable plugins" | **CONFIRMED-WITH-CORRECTION** | The 8 reproduce, but only 6 reach the planner prompt. The 2 `azure_*` names appear only in on-demand hints, which the evidence itself calls already conditioned. |
| Evidence A (multi-query + vague_term) | **CONFIRMED** | Re-ran both modes: byte-identical output, and the executor selection matches `transform.py:645-648`. |
| Evidence B (report_assemble → document) | **CONFIRMED** | Re-ran. The proposed fix verified by monkeypatch (see R11). |
| Evidence C (`not_for` cross-refs) | **CONFIRMED-WITH-CORRECTION** | Output byte-identical on re-run, and the instrument is controlled. The claim "binding prohibition and no legal alternative" for a type change is wrong: the source schema coerces. |

Counts: CONFIRMED 6, CONFIRMED-WITH-CORRECTION 5, OVERSTATED 1, REFUTED 0, UNVERIFIABLE 0.

---

## F4 — CONFIRMED-WITH-CORRECTION

**Reproduction (EXECUTED, `f4_probe.py`).** Every component of the captured response was fed through the production guard functions:

```
turn01 call 1   content: PASS   tool_wire/tool_sem emit_pipeline_proposal: PASS
                reasoning(meta): REFUSED category=token_shape surface=composer_planner_response
    hit credential_assignment 7799 '...Hmm the notes with "password": "Client emailed the admin password for the tria'
turn02 call 1   content: PASS   tool_wire/tool_sem emit_pipeline_proposal: PASS
                reasoning(meta): REFUSED category=token_shape
    hit credential_assignment 7446 '...I'll include some notes containing e.g. "password: hunter2" and "confidential"...'
```

Calls 0 of both turns pass. Each tripping reasoning has exactly one regex hit.

The production record builder also refuses on the captured turn01 call-1 response:
`build_llm_call_record(..., response=<captured>, credential_surface="composer_planner_response")` →
`CredentialMaterialRefused composer_planner_response token_shape`.

The emitted pipeline does contain notes like "The buyer emailed the portal password to the shared mailbox" and `blocked_patterns: ["(?i)password", ...]`, but none of that matches a pattern. So the guard really does fire on reasoning only.

**Corrections.**

1. **Citation.**
   - `pipeline_planner.py` ~L1831-1889 is `_parse_response_tool_calls`. It scans `content`, tool ids and names, and tool arguments. It does **not** scan reasoning.
   - L3671 is inside `record_provider_failure`, which is not relevant here.
   - The reasoning scan is `build_llm_call_record` → `require_no_credential_material_in_llm_metadata` (`web/composer/llm_response_parsing.py:759-770`; `web/credential_guard.py:91-126`). The planner calls it at `pipeline_planner.py:3913-3929` and catches it at `:3930-3963`.
2. **Trigger.** The pattern is `credential_assignment` (`contracts/credential_material.py:101-111`) and the category is `token_shape`.
   - The smallest tripping string is `password": "C`; even `password: h` trips (EXECUTED: `_classify_string`). `'"password": "Client emailed the admin pass'` is just context around the match, not the minimal window.
   - T2 tripped on a **different** string, `password: hunter2`. The README implies both turns had the same cause.
   - The breadth matters for severity: any reasoning line that writes `password:` followed by a non-space character fails the turn.
3. **Scope is wider than "the planner".** The same metadata scan runs for the tool-loop and advisor provider calls.
   - Tool loop and advisor scan: `provider_gateway.py:155-160` (`_require_no_credential_material_in_completion_fields` with `provider_metadata`).
   - Advisor catch: `advisor_checkpoint.py:620-626`.
   - So an advisor that reasons "e.g. password: x" also fails its review.

**User-facing text (CONFIRMED).**
- `s06-errors/turn01/terminal.json` and `turn02/terminal.json`: `http_status 422`, `error_type credential_material_rejected`, `detail` = "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
- READ: the frontend takes the nested `detail` string (`frontend/src/api/client.ts:490-497`), and `stores/sessionStore.ts:487` falls through to `error.detail` because there is no dedicated arm for this `error_type`.

**Severity.** HIGH is fair, given how broad the regex is.

---

## F6 — OVERSTATED

- **Type conversion is available at ingest.** The csv source coerces declared columns (`plugins/sources/csv_source.py:186`, `allow_coercion=True`). The live c01 T1 reply said: "Turn the quantity and unit price into real numbers. That part is doable: the CSV reader coerces declared columns on ingest" (`c01-core-orders/turn01/llm_calls.json`, call 1 content).
  - What is missing is mid-pipeline coercion, for example after an LLM step or a join.
- **Filtering is available.** Gates are core graph nodes, not policy plugins, and gate expressions support arithmetic (`core/expression_parser.py:98`, `ast.Mult`).
  - c01 T2 reply: "Keeping only orders over $100 — fine on its own".
  - That turn declined **because the line-total blocker from T1 remained**, and it offered to build the filtered list. "The follow-up … was declined too" is literally true but misattributes the cause.
- **Only arithmetic that writes a new field is unavailable.** That is `value_transform`; the LLM transform is correctly ruled out for exact values.
- **Correction.**
  - README Bottom-line table: change "Any arithmetic, type conversion or filter" to "Computing a new field (arithmetic), or changing a type after ingest".
  - F6 heading: change "cannot do arithmetic, type conversion or filtering" to "cannot compute new fields or coerce types mid-pipeline".
  - Severity: MEDIUM. It applies to default-allowlist deployments only; per evidence C the live demo deployment had 30 transforms.
- **Quote source.** "This is contradictory unless the digest is partial" is in `smoke-edit/turn01/llm_calls.json` call 0, not c01. c01 never states a contradiction (READ: no hit for `contradict` in c01 except unrelated prompt text). R8's verification step, "c01 reasoning no longer finds a contradiction", therefore passes today. It should name smoke-edit T1 instead, where call 0 wasted a `get_plugin_schema` on `type_coerce`/`value_transform` and got `plugin_not_enabled`.

---

## F14 — CONFIRMED-WITH-CORRECTION

**Reproduction (EXECUTED).** Re-ran `opus/agent-llm-wiring/a_multiquery_interp.py all_override|mixed`. Output was identical to the agent's:
- `set_pipeline success: True`
- all-override: both queries `accepted_in_prompt=False`
- mixed: `risk` (no own template) `accepted_in_prompt=True`

I checked that the script's selection logic matches the executor: `plugins/transforms/llm/transform.py:645-648`. `_query_templates` is built at `:547-550`, and `with_template_override` (`templates.py:256-268`) **replaces** the template string; it does not compose with the node-level one.

**Missed code path? None.**
- The only writers of a prompt template in `web/` are the node-level `prompt_template` writes: `pending_interpretation.py:819` and `:1000`, `tools/_common.py:2737`, `interpretation_state.py:3073` and `:3297`. Nothing writes `queries.*.template`.
- The resolve flow `pending_interpretation.py:1020-1078` calls only `_patch_llm_transform_prompt` and then the artifact hash.
- The tokens that would be needed are explicitly refused per query (`multi_query.py:236-250`).

**Additional integrity point (READ).** When no query uses the node template, `approved_prompt_artifact_hash_from_options` deliberately drops it (`interpretation_state.py:2805-2807`). The resolved value therefore does not even enter the attested hash in the all-override case. The record says "resolved", and nothing that runs carries the value.

**Corrections.**
1. **Title.** "never reaches multi-query LLM prompts" is too broad. Use: "reaches only queries without their own `template`; when every query has one, it reaches no prompt."
2. **"Nothing teaches the limitation" is understated: the teaching actively steers into the trap.**
   - `planner_authoring_aids.py:867-870` says "Reviewed slots belong in the node-level template".
   - The plugin's own rejection message (`multi_query.py:248-249`) says "Put reviewed slots in the node-level template".
   - Neither adds that the slot reaches only fallback queries.

Line citations are otherwise fine (`pending_interpretation.py:819`, `interpretation_state.py:2947`).

---

## F15 / Evidence C — CONFIRMED-WITH-CORRECTION

- **14 vs 9 (EXECUTED).** `REQUIRED_WEB_PLUGIN_IDS` has 14 entries. The doc's list is at `docs/reference/environment-variables.md:589-592`; the README says 587-592, and 587 is the lead-in sentence.
- **The 8 names (EXECUTED).** Re-ran `c_notfor_xrefs.py core`: output byte-identical to the agent's `c_core.out` (ignoring timestamp lines), with controls positive and negative. The distinct non-common-word names are exactly 8:
  - In the planner prompt (digest `not_for` plus `authoring_aids`): `type_coerce`, `value_transform`, `blob_csv_expand`, `blob_fetch`, `json_explode`, `blob_json_expand`. That is **6**.
  - `azure_prompt_shield` and `azure_content_safety` appear only in on-demand `get_plugin_assistance` hints that are already conditional ("only when discovery lists it"). They are not defects.
  - Correction: "the planner prompt names 6 unavailable plugins; 2 more appear in already-conditioned on-demand hints."
- **Evidence C, "a default deployment offers a binding prohibition and no legal alternative" for a type change.** Wrong for type changes at ingest; see F6.

---

## Remediation comments

### R6 (F4)

Is option (a) actually safe? I traced every place reasoning goes (READ).

- **Not replayed for `reasoning_content` / `reasoning_details`.**
  - The assistant replay message is `role`, `content`, `tool_calls`, plus native `thinking_blocks` only (`pipeline_planner.py:1987-2019`).
  - The captured s06 T1 call-1 request confirms it: message 3 has keys `['content','role','tool_calls']` and no reasoning. DeepSeek accepted that.
- **Not durably persisted.**
  - The session-visible LLM-call envelope is an explicit allowlist that excludes every reasoning field (`web/composer/audit.py:435-485`).
  - The private drafts (`audit_storage.private_diagnostic_drafts`, wired at `sessions/composer_turn.py:731-746`) carry `private_request` (the request side), `private_failure` and received tool-argument receipts. `capture_received_response` (`private_diagnostic_facts.py:201-286`) captures tool arguments only.
  - `provider_telemetry.py` reads only status, latency and totals.
  - Reasoning lives only on the in-memory `ComposerLLMCall`. That makes the guard's own docstring, "can be audited or replayed" (`credential_guard.py:104`), stale for DeepSeek/OpenRouter reasoning.
- **Not viable for Anthropic native `thinking_blocks`.**
  - They *are* replayed (`pipeline_planner.py:4420`, `:4609`, `:4676`, `:4822`, `:4880`) and are signature-bound.
  - The planner already refuses to replay native thinking alongside rewritten arguments (`:2017-2018`).
  - So (a) must be limited to `reasoning_content` / `reasoning_details`. A hit in `thinking_blocks` must still fail, or end the native-thinking chain.
- **Scope gap.** (a) must also cover the tool-loop and advisor paths (`provider_gateway.py:155-160`, `advisor_checkpoint.py:620`), not only the planner.
- **It reverses a pinned contract.** These tests pin the current refuse-on-reasoning behaviour and would need rewriting:
  - `tests/unit/web/test_credential_guard.py:520-560`
  - `tests/unit/web/composer/test_pipeline_planner.py` ~L5385
  - `tests/unit/web/composer/test_compose_loop_persistence.py:796-806` ("refuses atomically before any effect")

  This is correctly labelled "needs operator decision".
- **On (b) and (c).** (b) is weak: telling a model what not to *think* is unreliable, and the breadth of the regex makes recurrence likely. (c) is cheap and independent: `to_payload` already knows `surface`, so per-surface copy for `composer_*_response` needs no guard change. I recommend (c) now and (a) restricted to non-replayed reasoning.
- **Invariants.** No composer-invariant issue.

### R8 (F6)

- The direction is right, but "apply at digest render time" covers only one of three carriers. The agent's own evidence lists `authoring_aids` `llm_output_contract.rules[5]` (`planner_authoring_aids.py:825-829`) and the on-demand hints `field_mapper[22]`, `reference_join[5]` and `line_explode[3]`. The static skill pack's repair table, which is policy-independent, also names `type_coerce`.
- `not_for` is free prose, so filtering clauses at render time means string surgery. A smaller and safer fix: append a policy-aware suffix in the digest ("not enabled here: type_coerce, value_transform; an operator can enable them") instead of editing the prose.
- The verification target must be smoke-edit T1, not c01.
- This does not change F6's user outcome. The refusal is correct; enabling the plugins is the real demo fix.

### R11 (F11, F15)

- **Verified (EXECUTED, `f11_patch.py`).** I monkeypatched `ReportAssemble.output_semantics` to declare `STR` + `UNCONSTRAINED` with `content_kind=UNKNOWN` (document has an empty `accepted_content_kinds`). report_assemble → document went from `semantic_warnings=1` to `semantic_warnings=0`, still `is_valid=True`.
- **Side effect.** Under the same patch, report_assemble → **text** sink changes from an advisory WARN to a hard `semantic_contract_violation` error, because text accepts only `COMPACT` (`text_sink.py:348-351`). That is arguably correct, since text diverts rows with CR/LF at runtime. But today's single-line configurations would be rejected at validation (no title, `join_with` without a newline, single-line rows). Under the 0.8.5+ compatibility posture this needs an explicit statement and a test.
- **Verification step.** "s08 / s14 validate with no high warnings" is a weak check: s14 uses a **json** sink and already shows no semantic warning (`s14-report/turn01/state.json`). Use s08 plus an in-process report_assemble → document edge test.
- **Example fix.** The document example is correct on its own (a single field upstream). The fix should add a report_assemble-shaped variant using `flexible`/`observed`, not just change the mode.
- **Invariants.** No invariant concern.

### R13 (F14)

- Rejecting at staging is right, and it is validation, so no invariant concern. Put it in `vague_term_wiring_count` (the single source every gate uses) **and** in the resolver backstop (`pending_interpretation.py:790-802`).
- It still leaves the mixed case semantically wrong. The approved definition of "eligibility" reaches only the fallback query (`risk`), not the query that judges eligibility. R13 should also require that the reviewed slot's node template is used by the query or queries the term concerns. If that cannot be determined, at least warn on the prompt card.
- The alternative, "write into every query template that carries the slot", is **not S**. Per-query templates are forbidden from carrying slots (`multi_query.py:236-250`), so it needs a per-query parts model and lifting that rejection.
- The teaching line must also replace the existing "Reviewed slots belong in the node-level template" lines (`planner_authoring_aids.py:867-870`, `multi_query.py:248-249`), not just add one line.

---

## Missed issues

1. **The F11 warning class is wider than report_assemble.** s12 live (`s12-json-explode/turn01/state.json`) shows the same HIGH warning that nothing in the session can clear: "'json_explode' node 'explode_items' requires field 'items' (json_explode.array_field.list) but upstream producer 'source' (json) declares no semantic facts". So any producer without `output_semantics()` that feeds a `unknown_policy=WARN` consumer shows a HIGH ⚠ in plugin-developer wording, including the json source → json_explode pairing. R11 fixes one instance only.
2. **F4 also affects advisor and tool-loop calls**; see the F4 correction (3).
3. **The guard's docstring is stale** (`credential_guard.py:104`, "can be audited or replayed"). For `reasoning_content`, neither happens in the current web tree. The operator should know this when deciding R6: the hard-fail protects an in-memory field only.
