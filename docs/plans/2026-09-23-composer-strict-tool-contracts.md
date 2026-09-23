# Composer planner: strict tool contracts — implementation plan

- **Date:** 2026-09-23
- **Goal (John):** "ultimately we want a strict contract for every tool call" made by the Web Composer planner.
- **Status:** S0 implemented on `design/strict-tool-contracts` (not merged; the full-suite gate and S0 acceptance
  items 2, 4 and 5 are owed, see "S0 review fixes" below). S1 onward is plan only.
- **Code base the citations use:** `path:line` citations were measured at `6d8f7f729`. The branch
  `design/strict-tool-contracts` was then fast-forwarded to `release/0.8.1` at `c6e12fc44`. **Python `src` changes**
  in `6d8f7f729..c6e12fc44` are limited to `control_messages.py`, `sessions/models.py` and `sessions/schema.py`, so
  citations into those three files may be off by a few lines (for example, `SESSION_SCHEMA_EPOCH` is now **66** at
  `models.py:362`, and the `tool_calls` column moved from `:699` to `:703`). Re-check any `path:line` with `grep -n`
  before editing.
- **Evidence:** the design lane notes are in `.claude/lanes/strict-tool-contracts/` in the worktree. That directory
  is gitignored, so it is lost when the worktree is removed. It holds `understand-{providers,schemas,runtime,options}.md`,
  three `design-*.md` files, two of the three judge notes (`judge-reliability.md` = judge 1, `judge-3.md`), and the
  instruments cited in §2. S1's new `strict_profile.py` (first S1 file bullet) promotes the main instrument into a
  tracked module with tests.
- **Base design:** `provider-risk`, which 2 of the 3 judges preferred (scores 86 vs 84 vs 43). The scores come from
  the judging round's reports as relayed to the lead; judge 2's notes are **not** in the lane, so the three-way scoring
  cannot be rebuilt from the lane alone. It is combined with `envelope-first`'s mechanics and trimmed for a single
  maintainer (§1.4).

---

## 1. Decision summary

### 1.1 What "strict" means here

There are two layers, and only one of them decides admission.

1. **S, the semantic contract. It is the authority.** For each tool, S is:
   - the flat registry schema (`tools/_dispatch.py:309` `get_tool_definitions()`);
   - the tool's pydantic arguments model;
   - for tools that take options, plugin prevalidation (`PluginConfig`, `extra="forbid"`).

   The server enforces S on every call, on every provider, whatever the provider's strict mode does. It is already
   closed and enforced for 40 of the 42 tools: `execute_tool(validate_arguments=True)` runs Draft 2020-12 over the
   closed-root flat schema (`_dispatch.py:499`, `_closed_root_schema` at `:436`) and then the model. S0 closes the
   last two carve-outs (`request_advisor_hint`, `request_interpretation_review`) and the 6 hand-rolled MCP session
   tool definitions. **S alone decides whether a call is admitted.**
2. **W, the wire projection.** W is a static rendering of S that is derived per *dialect* and sent to the provider.
   It is built once at import. It never depends on the deployment, principal, plugin snapshot or state. Only the
   dialect and the per-tool `strict` flag vary, and they are resolved **per route**: from the effective model and
   api_base of the call actually made. There are two routes today: the planner route (`composer_model` + planner
   endpoint) and the pipeline planner's escape-hatch route (`composer_advisor_model` + advisor endpoint, S1). The
   provider's strict mode is a
   **Tier-3 promise**. We record it and measure it (`strict_sent` and `wire_conformant` on every invocation), and
   we never rely on it for admission.

"A strict contract for every tool call" is therefore delivered as:

- every call is admitted only by a closed S;
- every call is classified against the W it was sent under;
- every tool that a route can grammar-constrain is sent `strict: true`. That is 32 tools from S1, and the other 10
  after ruling R1.

### 1.2 Options strategy

The 10 option-bearing tools are `set_source`, `patch_source_options`, `set_source_from_blob`,
`set_source_from_blobs`, `set_pipeline`, `upsert_node`, `splice_transform`, `patch_node_options`, `set_output` and
`patch_output_options`. They contain open plugin option or patch objects. OpenAI strict mode and Anthropic strict
mode both require `additionalProperties:false` on every object, so these 10 tools **cannot be strict in any
dialect as they are shaped today**. The pipeline-planner terminal `emit_pipeline_proposal` has the same problem.

- **Until R1 is ruled**, all 11 are sent with an explicit `strict: false` on routes that recognise the key. Their
  contract is S, which is already closed on the server.
- **R1 is decided on data from S0**, not now. S0 makes shape errors and value errors distinguishable, including the
  plugin-option rejections that are recorded today as SUCCESS. If shape errors dominate on these tools, the candidate
  carrier is `options_json`/`patch_json` JSON text plus pair-encoded structure maps. It ships only if a
  tutorial-canary regression check passes (S2). If value errors dominate, no grammar would have prevented them, and
  these tools stay `strict:false` with S as their contract.

### 1.3 Why this shape

- A schema that varies per request cannot be proven at boot. So W is static, and plugin option schemas stay in
  tool *results* (`get_plugin_schema`) and in server validation.
- A provider that ignores `strict` looks the same as one that enforces it, unless each call is checked against what
  was sent. So `wire_conformant` is recorded on every call. It is also the only wire fact left after decode:
  `ComposerLLMCall` has no arguments field (`contracts/composer_llm_audit.py:148-180`), and
  `_replace_llm_tool_call_arguments` rewrites transcript arguments (`tool_batch.py:433-467`).
- A wire schema used as an admission gate would reject calls on routes without a grammar that are accepted today.
  So W only classifies calls. It never admits or rejects them.

### 1.4 What was taken from each design and what was dropped

| Taken | From |
|---|---|
| W classifies and never admits; `wire_conformant` on every invocation; an explicit boolean `strict`; `anyOf` for nullable enums and never `null` inside an `enum`; routes that cannot carry strict keep today's shape; a server-only constraint ledger rendered into descriptions; a CI wire-fidelity matrix with known-negative rows | provider-risk |
| One walk emits both the wire schema and its decode plan; a static list at import, where only the flag varies; S0 counts SUCCESS-status rejections by `error_code`; `schema_shape`/`schema_bound` split mechanically from the keyword allowlist; no `require_parameters`, `parallel_tool_calls` or `tool_choice` on the planner; the tutorial-canary acceptance gate for any options carrier | envelope-first |
| A limits report at build time that fails closed; a tri-state detector before any null→absent decode inside options; error pointers re-encoded from semantic form to wire form | per-plugin |

**Dropped, or moved off the critical path:**

- **All 42 tools sent with `strict:false` before any probe** (provider-risk S1). This could switch off OpenAI
  Responses' implicit normalisation, which today's omitted key allows (it arrives as `strict:null`). It is also an
  unprobed wire change on custom gateways. Instead, the first wire change sends `true` on the 32 tools, and the
  tools-bearing boot probe ships in the same slice and runs whatever the setting is.
- **`composer_strict_tools` with no default.** It gets the default `preferred`.
- **`require_parameters` on the planner.** It does not select on tool-level strict. With `seed`/`temperature` set it
  also shrinks the deepseek-v4.1-flash pool from 24 endpoints to 13, or 9 with structured outputs. That is judge-1's
  count (`judge-reliability.md:17`: seed+temp 13, plus structured_outputs 9); the endpoint data is in §2.2.
- **Posture state machine, enforcement canary, per-call LiteLLM `Logging` capture and endpoint pinning.** These
  move to optional S4, which is gated on measuring whether the canary can discriminate on the deployed model at all.
- **The `anthropic_strict` dialect and 20-tool packer.** Moved to optional S5: it reaches only unflagged Bedrock
  Claude models and buys grammar on small tools that rarely fail.
- **envelope-first's "Bedrock unchanged" claim (§2.3) and "raw wire attributable" claim (§3.2).** Both are false,
  as shown above.

### 1.5 Rejected alternatives

**Per-plugin typed option schemas in tool definitions (design `per-plugin`, layout G1).**
- Its runtime order makes the wire schema an admission gate (`WIRE_SCHEMA_VIOLATION`). Sending the same all-required
  schema without strict to Claude paths, and to the 10 of 24 deepseek endpoints without structured outputs,
  would reject calls that are accepted today.
- It classifies `prompt_template_parts` as server-owned, but the planner authors it (`tools/generation.py:1325`,
  `tools/transforms.py:1667`).
- Its boot probe forces a tool call, which Anthropic rejects when extended thinking is on, and fails boot when a
  stochastic output is invalid.
- Tool tokens grow 2.7× (required core) to 7.4× (full registry).
- set_pipeline reaches depth 12 against OpenAI's documented 10.
- The schemas vary per principal and snapshot, so no boot probe sees what a given user gets.
- Kept from it: the limits report, the tri-state detector, and the key-classification table idea (with corrected
  membership) as groundwork if typed options are ever pursued.

**provider-risk as written.**
- It sends all-`false` in S1 with probe P1 skipped under `off`.
- Its setting has no default.
- It keeps `require_parameters` on the planner.
- Its baseline does not see `plugin_options_invalid`.
- It loads the canary, posture machine and Anthropic dialect up front, before anything shows that they can
  discriminate. Its principles are kept (§1.4) and its machinery is deferred.

**envelope-first as written.**
- It sends one OpenAI-shaped W to every transport, including Bedrock, where `enum:[…,null]` loses its null, so a
  promoted optional enum becomes required with no null option.
- Its only enforcement signal is a `schema_shape` rejection. An omitted required-nullable key is decoded and
  admitted without trace, so a forwarding route that ignores strict cannot be seen.
- Its mechanics are kept (§1.4).

**Make the flat registry itself strict-shaped (one literal schema).**
- MCP clients would have to send every key, and `_OmittableString` would reject their nulls.
- `SetPipelineArgumentsModel`'s exactly-one-of-source/sources check is presence-based (`model_fields_set`), so
  every set_pipeline call would be rejected.
- `set_metadata.patch` treats an absent key as "unchanged", and merge-patch treats `null` as "delete". Both would
  change meaning.
- The required-paths walker raises `KeyError 'type'` on `anyOf` properties (runtime lane).

**Server validation only, no provider strict.** S is already that contract. It does not give John's goal of
constrained sampling where a route can offer it, and it leaves no way to measure what strict would change. S0 is
most of this option, and S1 adds the grammar at a measured cost.

---

## 2. Measured baseline

Environment for every command (worktree imports; verify both `__file__` values point into the worktree):

```bash
W=$(git rev-parse --show-toplevel)          # run from inside the strict-tool-contracts worktree
export PYTHONPATH=$W/src:$W/elspeth-lints/src
$W/.venv/bin/python -c "import elspeth,elspeth_lints;print(elspeth.__file__, elspeth_lints.__file__)"
L=$W/.claude/lanes/strict-tool-contracts
```

The worktree's `.venv` is a symlink to the main checkout's venv, so an exported `PYTHONPATH` is what makes the
worktree's code the one imported.

### 2.1 Tool list and strict gaps

Instrument: `$L/prototype_projection.py`. It first runs `check_strict.run_controls()`: 2 negatives give 0 rows, 11
synthetic positives fire, and one mutation goes red. It then loads the live list through
`ComposerServiceImpl._get_litellm_tools()` (`service.py:7719`), which is exactly what the web planner receives.

```bash
$W/.venv/bin/python $L/prototype_projection.py > /tmp/<lane>/proto.log 2>&1; echo "exit=$?"
```

I re-ran it on 2026-09-23: exit 0, `controls failures=0`, and apart from byte/time lines it is byte-identical to
`$L/prototype_projection.log`.

| Measure | Value |
|---|---|
| Web planner tools | 42. `_dispatch.py:312` says "43 tools", which is stale |
| Already strict-clean as sent | 21 (schemas lane, `check_strict.py`) |
| Strict candidates (no free-form object anywhere) | **32** |
| Option-bearing | **10** (list in §1.2) |
| S1 promoted properties (optional → required+nullable) | 13, each with a `STRIP_NULL` decode node (§3.3 rule 3); the other 2 are `upsert_edge` and `get_plugin_assistance` (1 each), whose flat schema already accepts null. **11** of the 13 are omission-only today, so the flat schema rejects null and the strip is load-bearing: `create_blob.description`, `wire_blob_inline_ref.encoding`, `clear_source.source_name`, `get_pipeline_state.component`, `list_models.provider`, `list_models.limit`, `set_metadata.patch.name`, `set_metadata.patch.description`, `request_advisor_hint.schema_excerpt`, `request_interpretation_review.llm_draft`, `wire_secret_ref.target_id` |
| S1 keywords left on the 32 after the allowlist | `format:uuid` 1, `minimum` 1, `maxItems` 2 |
| Full end state (carriers + pair maps) | 87 optional properties promoted, 28 of them omission-only; 13 option/patch carriers; 5 structure maps (`set_pipeline` `sources`, `nodes[].routes`, `nodes[].branches`; `upsert_node` `routes`, `branches`); 0 structural violations |
| Bytes (instrument's compact form) | 32 candidates 28,464 → 28,814 (+1.2%); all 42 at end state 63,829 → 66,404 (+4.0%) |
| Unsupported keyword today | one `not`, at `splice_transform /properties/node/properties/id/not` |
| Root unions on the web list | 0. set_pipeline's `oneOf` sits at `/properties/pipeline/oneOf` because of the envelope (`service.py:7738-7744`) |

Anthropic budget. Pass the dump explicitly: `anthropic_pack.py` loads `anthropic_budget.py` from a *scratchpad*
path (line 2), so edit that path first.

```bash
cd $L && $W/.venv/bin/python anthropic_budget.py web_planner_tools.json    # "controls ok", total_optional=87 total_union=3
cd $L && $W/.venv/bin/python anthropic_pack.py web_planner_tools.json      # control True; candidates 22; selected=20 optional=13 union=0
```

Both were re-run on 2026-09-23 with the results shown. Anthropic's per-request caps are 20 strict tools, 24
optional parameters and 16 union parameters, counted across all strict schemas. They were quoted verbatim from the
downloaded doc by the providers lane; non-strict tools do not count. Under those caps, set_pipeline (39 optional)
and upsert_node (21) can never be strict on Claude routes as currently shaped.

### 2.2 Provider support matrix (LiteLLM 1.102.0)

Version: `python -c "import importlib.metadata as m;print(m.version('litellm'))"` → `1.102.0`.

The matrix comes from the providers lane's loopback recorder, which sends the real 42 tools with `strict` added
through each LiteLLM path to a local server. No keys are needed. Instruments: `$L/providers-evidence/wire_probe.py`,
`real_tools_probe.py`, `enum_trace.py`, with results in `wire_probe.json` (6 paths: openai + custom api_base,
openrouter, azure gpt-4.1, azure gpt-5.5, anthropic, bedrock claude converse). Rows marked **code-read** were derived
from LiteLLM source, not recorded. I did not re-run the recorder. **S1 promotes it to a tracked CI test
(`test_wire_fidelity_matrix`)**, which also records the code-read rows.

| Route | `strict` reaches the provider | Schema caveats | Plan transport |
|---|---|---|---|
| `openrouter/<non-anthropic>` | yes, verbatim | none | `FORWARDING` |
| `openai/` + api_base host `openrouter.ai` | yes | non-Python-`re` `pattern` dropped (LiteLLM `gpt_transformation.py:448-462` runs `drop_non_python_regex_patterns` for provider `openai` on any non-api.openai.com base; code-read, not recorded) | `FORWARDING` |
| `openai/` or **bare model name** + custom api_base that is not OpenRouter (a gateway, including ELSPETH's own `gateway/`) | yes, and **ELSPETH's own gateway rejects it with HTTP 400** (see below) | non-Python-`re` `pattern` dropped | `NONE` unless the operator opts in (S1) |
| hosted `openai/` chat, `azure/` chat | yes | a root `oneOf/anyOf/allOf` is flattened (Azure unconditionally); non-Python-`re` `pattern` dropped. The hosted-openai row is **code-read** (`understand-providers.md` §3); `wire_probe.json` has no hosted-openai key | `ENFORCING` |
| Responses bridge (hosted openai or azure, gpt-5.4+, with function tools; this includes default `composer_model="gpt-5.5"`, `config.py:256`) | yes. **When omitted, LiteLLM sends `strict:null`**, which OpenAI "attempts to normalize" | non-Python-`re` `pattern` dropped (`llms/openai/responses/transformation.py:407-416` always applies at least that) | `ENFORCING` |

When the bridge is taken, measured on 2026-09-23 by calling LiteLLM's `responses_api_bridge_check` directly:
`gpt-5.5`/openai with tools and no effort → `responses`; the same with no tools → `chat`; with an explicit
`reasoning_effort="none"` → `chat`; `azure` with tools, effort unset or `low` → `responses`; openai + a custom
gateway api_base, effort unset → `chat`, effort `low` → `responses`; control `gpt-4.1` with tools → `chat`. ELSPETH
never sends the literal `"none"`: `apply_reasoning_kwargs` (`composer/reasoning.py:30-55`) adds nothing for effort
`none`, and adds nothing at all for bare and `openai/` model names (the `elspeth-9a46553771` gateway comment). So on
hosted OpenAI and Azure gpt-5.4+, **the bridge is selected by the presence of function tools**, whatever
`composer_discovery_reasoning_effort` says. On a custom gateway api_base it is never selected, because ELSPETH sends no
effort to those model names.

ELSPETH's own gateway. `gateway/src/elspeth_llm_gateway/core/contract.py:49-57` `ChatFunctionDef` is
`extra="forbid"` with only `name`, `description` and `parameters`; `core/app.py:286-288` turns the `ValidationError`
into `INVALID_REQUEST`, which `errors.py:42` maps to HTTP 400. Measured: `ChatRequest.model_validate` with
`tools[0].function.strict=True` → `extra_forbidden` at `('tools', 0, 'function', 'strict')`; `strict=False` → the
same; control with no key → accepted. `tests/integration/web/composer/test_composer_against_gateway.py` drives this
gateway with the bare model `gpt-5.5` + api_base, which LiteLLM resolves to provider `openai`. The same class of
defect (`max_tokens` → `max_completion_tokens`) once made boot fatal against this gateway.
| `bedrock/anthropic.*` not flagged `bedrock_converse_supports_strict_tools:false` | `toolSpec.strict` forwarded | every Python `None` stripped (`enum:[…,null]` loses null); `anyOf:[…,{type:null}]` survives | `NONE` (S5 may add a dialect) |
| `bedrock/*` flagged (Opus 4.7/4.8), non-Claude | no; root `additionalProperties` also dropped | as above | `NONE` |
| `anthropic/` native (default `composer_advisor_model`, `config.py:361`) | **no, silently dropped** (`_map_tool_helper` never reads it) | root allow-list drops root combinators | `NONE` |
| `openrouter/anthropic/*` | stripped unless `x-anthropic-beta: structured-outputs-2025-11-13` is sent | — | `NONE` |
| `hosted_vllm/`, `vertex_ai/`, `gemini/`, `watsonx/`, `fireworks_ai/`, `xai/`, `sap/`, anything unrecognised | no | strict and `additionalProperties` stripped on several | `NONE` (fail closed) |

The set_pipeline `{"pipeline": …}` envelope survives all 6 measured paths unchanged.

OpenRouter endpoints, from the public endpoints API. I recounted them on 2026-09-23. The control is a nonexistent
parameter, which matches 0 endpoints, while `tools` matches all of them.

```bash
cd $L/providers-evidence && for f in ep_*.json; do echo "$f total=$(jq '.data.endpoints|length' $f) \
 so=$(jq '[.data.endpoints[]|select(.supported_parameters|index("structured_outputs"))]|length' $f) \
 ptc=$(jq '[.data.endpoints[]|select(.supported_parameters|index("parallel_tool_calls"))]|length' $f) \
 ctl=$(jq '[.data.endpoints[]|select(.supported_parameters|index("zzz_not_a_param"))]|length' $f)"; done
```

| Model | endpoints | tools | structured_outputs | parallel_tool_calls |
|---|---|---|---|---|
| `deepseek/deepseek-v4.1-flash` (the deployed planner, per lead observation) | 24 | 24 | 14. The first-party `DeepSeek` endpoint is among the 10 without | **0** |
| `z-ai/glm-5.3` (advisor) | 34 | 34 | 26 | 1 |
| `openai/gpt-5.5` | 7 | 7 | 6 | 0 |

`require_parameters` is documented only for top-level parameters, so it does not select endpoints on tool-level
strict. Sending `parallel_tool_calls` with it leaves 0 deepseek routes.

OpenAI strict rules (official docs, as quoted by the providers lane):
- every property is required, and every object has `additionalProperties:false`;
- supported: `anyOf` (not at the root), `pattern`, 9 `format` values, numeric `minimum`/`maximum`/`multipleOf`,
  `minItems`/`maxItems`;
- `minLength`/`maxLength` appear only in the fine-tuned exclusion list, so treat them as unsupported;
- `default` and `oneOf` rejection is community-reported;
- limits: 5000 properties, 10 nesting levels, 120k characters, 1000 enum values.

### 2.3 Argument-error data

| Fact | Command / instrument | Result |
|---|---|---|
| Hand-written error labels | `grep -n '"TypeError"\|"ValueError"' src/elspeth/web/composer/{tool_batch,service}.py` | **6 sites**: `tool_batch.py:929` (non-object), `:967/:987/:994` (set_pipeline envelope: audit args, arg-error record, tool outcome), `service.py:7841` (advisor schema: TypeError/ValueError picked by pydantic type), **`service.py:7860`** (advisor prompt-budget cap, `"ValueError"`). The judges counted 5 and missed `:7860` |
| Other literal `error_class` labels naming a class that was not raised | `grep -n 'error_class="\|"error_class": "' src/elspeth/web/composer/{tool_batch,pipeline_planner}.py` | `tool_batch.py:1170,1177` `"MissingRequiredPaths"` (no such class: `grep -rn "class MissingRequiredPaths" src/` is empty, yet the name is in `redaction.py` `_SAFE_ARG_ERROR_CLASSES` at `:194-206`); `tool_batch.py:1818,1900` `"TimeoutError"` from deadline checks that raise nothing; `pipeline_planner.py:2544,2741` `"ValidationError"` and `:2990` `"SchemaValidationError"`, hand-written planner feedback payload labels. The two-string grep in the row above cannot see these |
| Persisted ARG_ERROR discriminant | read `redaction.py:194-207`, `:370-400` | only `error_class` from a 10-name allowlist, else `<redacted-arg-error-class>`, plus counts. `ToolArgumentError.code` (5 closed values, `protocol.py:778-786`) is dropped |
| **Plugin-option rejections are SUCCESS rows, and their `error_code` is redacted** | scratch probe: `_failure_result(state, "Invalid option 'secretvalue'", error_code="plugin_options_invalid")` → `redact_tool_call_response("upsert_node", …)` | raw entry `error_code: 'plugin_options_invalid'`; **persisted `error_code: '<redacted-response-text>'`**. Positive control: `severity: 'high'` survives. `success: False` survives. Negative control (`error_code=None`): the key is **absent in raw and `null` in persisted** (the redactor writes `error_code`, `contract`, `row_union_schema`, `coalesce_union_type` and `rejected_component` as `null`); Appendix A's `coalesce(…, 'uncoded')` already handles the null. So "rejected" can be counted today, but *why* cannot |
| Rejection sites with a code | `grep -n 'error_code="' src/elspeth/web/composer/tools/*.py` | `plugin_options_invalid` at `transforms.py:745,1768,1963`, `sources.py:977,1160,1422,2031`, `outputs.py:171,272` (set_output, patch_output_options) and `tools/sessions.py:1162,1346,1492,1592,1650,1677` (set_pipeline; `:1650` conditional); `interpretation_requirements_invalid` at 8 sources sites (`sources.py:960,1072,1088,1252,1406,1471,1951,1993`). Many `_failure_result` calls carry no code (e.g. `transforms.py:738`) |
| Emitted codes outside the guidance catalogue | live membership test against `tools/generation.py` `_VALIDATION_GUIDANCE_BY_CODE` (positive control `plugin_options_invalid` → True; negative `zzz` → False) | **not in the catalogue**: `edge_not_lowerable` (`transforms.py:1421`, `state.py:3463`), `edge_route_conflict` (`transforms.py:1440`, `state.py:3485,3495`), `interpretation_review_pending` (`service.py:2733`), `output_name_invalid` (`state.py:507`, state validation `_routing_label_errors`, so it can surface on any mutating tool), `prompt_template_parts_required` (`transforms.py:1669`, inside `_execute_patch_node_options`, an option tool), `round_trip_unavailable` (`tools/sessions.py:2290`), `runtime_preflight_not_run` (`generation.py:4034`). Probe: `prompt_template_parts_required` and `output_name_invalid` persist as `<redacted-response-text>` exactly like `plugin_options_invalid`. The list comes from a literal scan, so it is a lower bound: codes passed through a variable (e.g. `transforms.py` `_post_mutation_invariant_error` → `error_code=error_code`) are not in it |
| Archived dev sessions | **lead-measured, not re-verified here** | 14 sessions, 151 planner tool calls, 4 ARG_ERRORs (get_plugin_schema, set_pipeline ×2 including the envelope rejection, request_interpretation_review). Stored text is redacted, so shape and value cannot be told apart. The sample is too small to judge any slice on. Appendix A has the census query for accruing new data |

### 2.4 Other baseline facts

- Planner boot probe (`boot_probe.py:66-76`): sends **no tools** and `max_tokens: 16`, and **does not call
  `apply_reasoning_kwargs`**. `_call_llm` does call it (`service.py:7778`). On hosted OpenAI and Azure gpt-5.4+ the
  Responses bridge is selected by the presence of **function tools** (§2.2, measured), not by ELSPETH's reasoning
  kwarg, so the probe's missing tools are what keep it off the production route there. The missing reasoning kwarg
  matters on other routes (OpenRouter `reasoning`, and Anthropic/Bedrock thinking, see S0).
  - Timeouts: planner 5 s, advisor 60 s (`app.py:194-195`, applied at `:704`).
  - A 400 raises `ComposerBootConfigError`. A transient failure is a warning.
- `_call_llm` (`service.py:7757-7792`) sends `model`, `messages`, `tools`, optional temperature/seed, reasoning
  kwargs and endpoint kwargs. It sends no `tool_choice`, `parallel_tool_calls` or `provider`.
  `composer_max_tool_calls_per_turn=16` (`config.py:315`).
- The pipeline planner is on the live freeform path. `service.py:4079-4113` routes an empty state with an explicit
  mutation and `guided_terminal is None` to `_plan_and_stage_empty_pipeline`.
  - Its discovery subset (`PLANNER_DISCOVERY_TOOL_NAMES`, `capability_skill.py:17`, 19 tools) does not intersect
    the 10 option tools (judge-3 check with a positive control).
  - Its terminal schema has 15 `not`, 28 `pattern`, 3 `propertyNames`, 13 `anyOf` and 1 `oneOf` (runtime lane).
- Persistence:
  - Tool invocations and LLM calls persist as JSON in `chat_messages` (`tool_calls` JSON column,
    `sessions/models.py:699`; the writer is `audit_storage.py:243-277`; LLM calls go through
    `src/elspeth/web/sessions/routes/_helpers.py` `_persist_llm_calls` (`:1914`) → `add_messages_atomic`).
  - The outcome reader at `sessions/routes/_helpers.py:540-600` reads by key membership.
  - So *adding* keys needs no DDL. S0 must still confirm that no reader rejects unknown keys (R3).
  - **LLM-call records are persisted through a closed key whitelist.** `composer/audit.py:341-375`
    `_LLM_CALL_PUBLIC_AUDIT_FIELDS` is a closed tuple, and `_public_llm_call_audit_payload` returns only those keys;
    every drain persists through `llm_call_audit_envelope` (`sessions/routes/_helpers.py:1947,2056,2132`). A new
    `ComposerLLMCall` field that is not added to that tuple is silently dropped: stored as absent and read back as
    NULL, not as an error. `tests/unit/web/composer/test_llm_finish_reason_audit.py:15-18` records the prior incident.
    Tool invocations are not affected (`audit.py:338` persists `invocation.to_dict()` whole).

---

## 3. Target contract rules and source of truth

### 3.1 Authority chain, per tool

```text
pydantic arguments model  ──(schema_contract directional walker: advertised may be looser, never narrower)──►
flat registry schema S (_dispatch.get_tool_definitions; MCP inputSchema; runtime Draft 2020-12 gate)
        │  project_tool()  — one walk, pure, at import
        ▼
WireTool{ name, description(+ledger text), parameters(W), decode_plan, strict_capable, ledger }
        │  strict flag stamped per transport at boot
        ▼
provider
```

- **S stays the single authority.** The flat registry, MCP, `test_tool_declarations` byte pins and the
  `schema_contract` walkers are unchanged in S0 and S1.
  - Generating S from the pydantic models is a desirable follow-on. It is not a prerequisite: it moves the pins in
    `test_tool_declarations.py` (93 collected tests, most of them byte-identity pins; the file also holds
    `TestAssertUniqueNames` and `TestDerivationHelpers`) and is independent of provider strictness.
- **W is derived, never hand-written.** A new faithfulness check (§3.4) runs at import, next to the `_dispatch.py`
  invariants, so a bad projection fails web boot.
- **Plugin options** are typed by `PluginConfig.model_json_schema`, disclosed by `get_plugin_schema` and enforced
  by prevalidation. They are never embedded in W.

### 3.2 Dialects

| Dialect | Used on | Optional properties | Keywords | `strict` key |
|---|---|---|---|---|
| `openai_strict` | `ENFORCING` and `FORWARDING` transports | required and nullable (§3.3 rule 3); decode strips null | allowlist (rule 4); the rest go to the ledger (rule 5) | explicit `true` on strict-capable tools, explicit `false` on the others |
| `none` | `NONE` transports, and any transport when `composer_strict_tools=off` | **today's bytes, unchanged** (flat plus the set_pipeline envelope) | unchanged | **omitted** |

`none` sends exactly what is sent today. That keeps Anthropic, Bedrock, custom gateways (including ELSPETH's own)
and unknown routes at zero wire change, and it avoids Bedrock's `None`-stripping altogether. An optional
Anthropic-shaped dialect is S5.

### 3.3 `openai_strict` projection rules

1. The root is `type: object` with `additionalProperties: false` and no root combinator. The set_pipeline
   `{"pipeline": …}` envelope moves out of `_get_litellm_tools` into the projection as one declared wrapper.
2. Every object that has `properties` lists all of them in `required` and has `additionalProperties: false`. A
   property with no `properties` whose `additionalProperties` is anything other than `false` makes the tool
   `strict_capable = False`: it keeps its flat shape (rules 3–6 are not applied) and goes on the wire with
   `strict:false`.
3. Every S-optional property becomes required and nullable:
   - scalars, arrays and objects use `type: [T, "null"]`;
   - **enums use `anyOf: [{…enum…}, {"type": "null"}]`, and `null` is never placed inside an `enum`**;
   - an existing `enum:[…, null]` (for example upsert_node `output_mode`/`scope_policy`) is rewritten to that form.

   Each promotion adds a `STRIP_NULL` node to the decode plan.

   **Omission guidance is rewritten at every promoted position.** Several promoted properties tell the model to
   *omit* the key, which contradicts a wire-required key in the same definition. Measured on the live list
   (case-insensitive `\bomit`), on S1 positions: `list_models.provider` ("Omit to get a provider summary…"),
   `get_plugin_assistance.issue_code` ("Omit or pass null…", and the tool description's "Omit ``issue_code`` (or
   pass null)"), `get_pipeline_state.component` ("omit component"), `request_interpretation_review.llm_draft` ("OMIT
   this when…"). S2 positions with the same problem include `upsert_node` `trigger`/`on_success`/`on_error`/
   `output_mode`/`policy`/`merge`/`expected_output_count` (and its tool description) and `set_pipeline`
   `nodes[].on_success`/`on_error`/`expected_output_count` (regex hits; confirm each by reading before S2). The
   projection rewrites "omit X" into "pass null for X"
   (or appends that sentence where a rewrite is ambiguous), pinned per dialect; `none` keeps today's text.
4. **Keyword allowlist, fail-closed.** Kept: `type, properties, required, additionalProperties(false), items, enum,
   const, anyOf (nested), description, pattern (Python-re-compilable and ECMA-safe), format (the 9 OpenAI values),
   minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf, minItems, maxItems`.
   - **Moved to the ledger:** `default, examples, title, not, oneOf, minLength, maxLength, uniqueItems,
     propertyNames, format:uri`, and every `composer_*` vendor keyword.
   - **A keyword with no rule raises at import.**
5. **Ledger.** For each tool, the ledger holds `(json_path, keyword, value)` for every keyword removed. Each entry is
   rendered as text in that property's `description` (for example, "at most 200 characters").
   - Soundness: every ledgered keyword must sit on a tool routed through the S Draft 2020-12 gate. After S0, that is
     all 42 tools.
6. **Limits report at build, fail-closed.** It reports properties, enum values, characters, depth and the strict
   tool count per request against the OpenAI limits (§2.2). Exceeding a limit raises at import.
7. `strict_capable` is true when the projected schema passes the promoted `strict_profile` checker (§4 S1, the new
   `strict_profile.py` file bullet).
   That is 32 tools in S1.
8. `$ref`/`$defs` are never emitted (advisor precedent, `advisor_output.py`).

### 3.4 Decode, encode and the faithfulness gate

- **Decode:** `decode_wire_arguments(tool, dialect, raw) -> (semantic, wire_conformant)`.
  - It is a Tier-3 boundary with an honest `@trust_boundary`.
  - Input: the decoded, **pre-unwrap** arguments dict, taken right after the non-object check (`tool_batch.py:910`).
    Decode therefore **absorbs the set_pipeline envelope unwrap**. The inline block at `tool_batch.py:961-1008`, with
    its three hard-coded labels, is deleted, and decode becomes the single Tier-3 boundary between the provider
    wire and the semantic shape.
  - Placement:
    - in `run_tool_batch`, **before** audit canonicalisation, the discovery cache, the required-paths walker and
      `execute_tool`;
    - in the pipeline planner, after `_parse_json_object` (`pipeline_planner.py:1581`) for discovery calls.
  - Steps:
    1. `wire_conformant` = the raw (still enveloped) arguments validate against the W that was actually sent
       (Draft 2020-12). **This is classification only. It never rejects.** On `none`, W is the flat schema plus the
       envelope, so the value equals S-schema validity.
    2. `ENVELOPE_UNWRAP` is the first plan node for set_pipeline, on both dialects. Anything other than exactly
       `{"pipeline": {…}}` raises `ToolArgumentError(category=wire_envelope)`. That is the one rejection that
       replaces today's inline block. The existing behaviour is kept: redacted audit arguments and the transcript
       rewrite through `_replace_llm_tool_call_arguments`.
    3. `openai_strict` only: follow the decode plan, and `STRIP_NULL` only at promoted positions. **It never recurses
       into an option or patch map**, where `null` means delete (`_apply_merge_patch`, `_common.py:1636`). A key the
       model omitted passes through unchanged, and S decides.
    4. Later slices only: pairs → map (reject duplicates) and carrier text → object. These, with the envelope, are
       the only wire-stage rejections (`wire_decode`).
  - It never inserts or chooses a value. Defaults stay on today's omission paths.
- **Encode:** `encode_semantic_arguments` is the exact inverse. It is used only by `_replace_llm_tool_call_arguments`
  (`tool_batch.py:433`), so the replayed transcript stays in the shape that was sent. `arguments_canonical` records
  the **semantic** form, following the envelope-unwrap precedent.
- **Faithfulness gate** (`assert_wire_projection_faithful`, run at import):
  - each strict-capable W passes `strict_profile`;
  - every wire-required, flat-optional property is nullable and has `STRIP_NULL`;
  - every flat-required property is wire-required and not newly nullable;
  - every dropped keyword is on the ledger and sits on an S-gated tool;
  - no description in a strict-capable W (property or tool level) instructs the model to omit a wire-required key
    (control: plant "Omit X" on a promoted property → red).
  - Pinned negative control: the existing directional walker pointed at W must go **red**.
- **Presence semantics are preserved.**
  - set_pipeline's exactly-one-of-source/sources check is by presence (`model_fields_set`). Decode running first
    keeps it correct: `{source:{…}, sources:null}` is accepted through the web wire and still rejected through
    MCP/flat. Both cases are pinned.
  - The 28 omission-only fields keep rejecting explicit `null` in S. Their pins stay true
    (`test_omitted_nonnull_fields_reject_explicit_null` and others).

---

## 4. Rollout

Rules for every slice:

- Each slice is one shippable, consistent state. No dual acceptance, no old wire form alongside the new one.
- Write the tests first and watch them fail (RED) for the right reason.
- Run targeted test files with `-n 0`, plus the whole-tree gates whose scanned inputs change.
- **The full-suite gate** (`scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest`)
  **is owed before each merge.** These slices change shared runtime and contracts. Run it only when the host has
  capacity; not while another session is running suites.
- Trust-tier lint: `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check
  --rules all --root src/elspeth`. Compare the corpus before and after; do not compare it to zero. Stage no
  signatures.
- Soft-mapping census: new `Mapping[str, Any]` signatures → `scripts/check_contracts.py --write-census` in the same
  commit.
- Attribute-contract and masquerade baselines: the new modules are free of `getattr`/`hasattr`, so these must not
  move. Verify.
- Composer invariants: no slice adds a provider call to any compose transition. New provider calls happen only at
  boot. The standing per-transition review trigger therefore applies only to the boot probe, and it is noted in each
  PR.
- **Boot-time budget.** The probes run one after another inside the ASGI lifespan, before uvicorn binds, so
  `/api/health` refuses connections for their whole duration. Today's worst case is 65 s (planner 5 s + advisor 60 s,
  `app.py:194-195`). The startup contract is 150 s (`deploy/azure-container-apps/workload.bicep:406-418`: 15 s × 10,
  failureThreshold capped at 10; `docs/runbooks/aws-ecs-deployment.md:2036-2046`: `startPeriod 150`, and "the
  approximately 90-second database budget excludes probe time"), which leaves about 60 s for **all** probes on a
  two-cold-cluster boot. No slice may raise the worst-case total probe time above that (see S0 boot-probe bullets).

### S0 — Instrument (no change to the tool wire)

Purpose: honest, closed error data that survives redaction, and a boot probe that proves the route production
actually uses. Without S0 no later slice can be judged.

Files:

- `src/elspeth/contracts/composer_audit.py`:
  - add `ToolArgumentErrorCategory` (StrEnum, §5);
  - add `error_category: ToolArgumentErrorCategory | None` to `ComposerToolInvocation` (required on ARG_ERROR,
    `None` otherwise);
  - **add** a `__post_init__` that validates `error_category` against `status`. The class has none today, and its
    docstring (about `:148`) says "No `__post_init__` freeze guard is needed and none is defined". Rewrite that
    paragraph: the new hook is a cross-field status/category check, not a freeze guard.
- `src/elspeth/web/composer/protocol.py`: `ToolArgumentError` gains a keyword-only `category`, canonicalised to the
  closed set. It stays `@final` and frozen.
- `src/elspeth/web/composer/tool_batch.py`: the sites at `:929` (non-object), `:967/:987/:994` (envelope), the
  `bounded_json_loads` failure, canonicalisation and the required-paths sites (`:1170,1177`, today's invented
  `"MissingRequiredPaths"`) all set their category. The honest class is `ToolArgumentError` where the site now
  constructs one. The deadline sites at `:1818,1900` (`"error_class": "TimeoutError"`, nothing raised) either get an
  honest owned label or are listed as deliberately out of scope in the producer census below; decide in S0, do not
  leave them silent.
- `src/elspeth/web/composer/service.py:7831-7863`: the advisor validator returns `model_validation` / `prompt_budget`
  instead of the `"TypeError"`/`"ValueError"` hand-labels.
- `src/elspeth/web/composer/tools/_dispatch.py`:
  - `_validate_tool_arguments` (`:499`) sets `schema_shape` or `schema_bound` from the first error's `validator`
    keyword, tested against `WIRE_KEYWORD_ALLOWLIST`;
  - `WIRE_KEYWORD_ALLOWLIST` is defined here in S0 as the single constant that S1's projection imports;
  - fix the stale docstring at `:312` (43 → 42, with the right breakdown).
- `tool_batch.py` (~`:1714-1733` advisor, ~`:2069` interpretation review): validate their arguments over S (closed
  root) before their pydantic models, which removes the two carve-outs. **`_validate_tool_arguments` cannot be
  called on them as it stands**: `_closed_root_schema` (`_dispatch.py:436-441`) reads `_TOOL_DEFS_BY_NAME[tool_name]`,
  and neither carve-out is in that map (measured: `request_advisor_hint` → False, `request_interpretation_review` →
  False, control `set_source` → True); `get_tool_definitions()` adds them as inline definitions
  (`_dispatch.py:317-320`). So S0 first builds one schema lookup over all 42 definitions that
  `get_tool_definitions()` returns (the same lookup S1's `_WIRE_TOOL_DEFS` needs), and `_closed_root_schema` reads
  that.
- `src/elspeth/web/composer/redaction.py`:
  - `redact_arg_error_response` (`:370`) persists `error_category`;
  - `_SAFE_ARG_ERROR_CLASSES` (`:194`) keeps only classes that a producer census test shows are still raised
    (`MissingRequiredPaths`, `TypeError` and `ValueError` are candidates to leave);
  - **add `error_code` to `_SAFE_PUBLIC_RESPONSE_TEXT_BY_FIELD` (`:150`), with membership in a closed registry of
    every emitted `error_code`**, not in the guidance catalogue `_VALIDATION_GUIDANCE_BY_CODE`. That catalogue misses
    at least 7 codes the tools emit, two of them on option tools (§2.3 row "Emitted codes outside the guidance
    catalogue"); gating on it would leave the R1 split for option tools partly blind.
  - The registry is a leaf module (e.g. `composer/error_codes.py`) that the producers, `generation.py` and
    `redaction.py` all import. That also avoids the load-order problem: `generation.py` imports `redaction` at load
    time (see the function-local import at `redaction.py:2722`). `_VALIDATION_GUIDANCE_BY_CODE` keys become a subset
    of the registry, pinned by a test.
- `src/elspeth/web/composer/audit_storage.py`: write `error_category` into the invocation envelope.
- `src/elspeth/web/composer/pipeline_planner.py:4715` and `:5031-5032` (`arg_error_payload_factory`, which also
  writes `"error_code": exc.code or "argument_error"`): use the category value instead of `exc.code or
  "argument_error"`. `_closed_planner_rejection_codes` (defined at `:1101`, called at `:1253`) rewrites any code
  outside `_PLANNER_SERVER_REJECTION_CODES` (`:1073`: `argument_error`, `canonical_schema`,
  `deferred_intent_claim`, `validation_error`) or `_CLOSED_VALIDATION_ERROR_CODES` to `"validation_error"`, so the
  category values **must be added to that closed set**, or they are silently collapsed. (Today's `exc.code` values
  such as `SCHEMA_VALIDATION` already collapse this way.)
- **Verify first (unmeasured):** does OpenRouter's top-level response `provider` field reach the object that
  `_admit_composer_llm_completion` sees through LiteLLM 1.102? Check with the loopback recorder (serve a canned
  OpenRouter-shaped response that carries `"provider": "X"`) or with one keyed call. If it is dropped, use
  `_hidden_params` or a response header if either carries it. If neither does, drop the per-endpoint split from the
  S1 acceptance and from Risk 2's detector, and say so here.
- `src/elspeth/contracts/composer_llm_audit.py`: add `provider_served: str | None`, the OpenRouter response
  `provider` field, admitted as Tier-3 in `_admit_composer_llm_completion`. This makes any per-endpoint signal
  attributable **only if it is persisted**:
  - `src/elspeth/web/composer/audit.py` `_LLM_CALL_PUBLIC_AUDIT_FIELDS` (`:341-375`) gets `provider_served`, or it is
    silently dropped at every drain (§2.4);
  - admission bounds it to a public-safe token before it is whitelisted: a closed OpenRouter provider-name shape
    (bounded length, restricted charset), with anything else recorded as a fixed `unrecognised` token;
  - test: a persisted-projection test following `TestSurvivesToThePersistedProjection`
    (`tests/unit/web/composer/test_llm_finish_reason_audit.py:185`), with a mutation control (remove the key from the
    tuple → red).
- Composer MCP `_SESSION_TOOL_DEFS`: the 6 hand-rolled definitions get `additionalProperties:false` roots.
- `service.py:7928-7932`: trace whether every compose call that reaches session-aware tools has a `session_id`.
  Then either correct the message (the builder does no filtering) or delete the unreachable premise.
- **Boot probe** (`boot_probe.py`, `app.py:194,704`):
  - extract the planner request-kwargs construction from `_call_llm` (`service.py:7766-7779`) into one function,
    and use it both in `_call_llm` and in the loop-list probe request, so that temperature, seed, **reasoning** and
    endpoint kwargs are identical;
  - extract the pipeline planner's own kwargs construction (`pipeline_planner.py:3925-3941`: its
    `max_tokens=budget_policy.max_completion_tokens`, `num_retries=0`/`max_retries=0`, and
    `apply_reasoning_kwargs` with a per-turn effort that is often the **candidate** effort, default `high`,
    `config.py:271`, rather than `_call_llm`'s discovery `low`) the same way, and build the planner-list probe
    request from it at candidate effort. Sending planner tools with `_call_llm`'s kwargs would not be the
    route the pipeline planner uses;
  - the tool-list builder: the probe runs in the ASGI lifespan (`_service_lifespan`, `app.py:543`, probe at
    `:688-770`), **after** `_create_app` has built `app.state.composer_service` (`app.py:1781`), and
    `_get_litellm_tools` (`service.py:7719`) never reads `self`. So the probe *could* call
    `app.state.composer_service._get_litellm_tools()`. The builder still becomes a module-level function of
    `(model, api_base, settings)`, for one reason only: probe/production parity of the tool list and the transport
    resolution (from S1) without reaching into a private method of the service. The service calls it with
    `self._model`, `self._endpoint_base_url` and `self._settings`; the probe calls the same function with the same
    settings. Test files that call `_get_litellm_tools` and move with the refactor (9, measured with
    `grep -rln _get_litellm_tools tests`): `integration/web/composer/test_bedrock_live_smoke.py`,
    `unit/web/composer/test_advisor_tool.py`, `test_compose_loop_carriers.py`, `test_compose_loop_envelope.py`,
    `test_dispatch_arms_characterization.py`, `test_planner_authoring_aids.py`, `test_provider_cache_markers.py`,
    `test_tool_schema_contract.py`, and `unit/web/test_composer_bedrock.py`. Keep `_get_litellm_tools` as a thin
    delegating method only if removing it would churn those tests for no gain;
  - the probe sends that exact loop list and, as a second request, `planner_tool_definitions()`. Sending tools is
    also what moves the probe onto the Responses bridge on hosted OpenAI/Azure gpt-5.4+ (§2.2);
  - `max_tokens: 16` stays for the loop-list request, because the planner branch never parses tool calls, and a 200
    with a truncated reply still proves acceptance. **Limit:** on Anthropic and Bedrock, LiteLLM drops extended
    thinking when `max_tokens` is at or below the minimum thinking budget
    (`llms/anthropic/chat/transformation.py:1296-1310` `cap_thinking_budget_to_max_tokens`, called from
    `bedrock/chat/converse_transformation.py:978` and the anthropic transformation `:1560`), while `_call_llm` sends
    no `max_tokens`. So on those routes a 16-token probe does not prove the thinking route. Either use a `max_tokens`
    above `ANTHROPIC_MIN_THINKING_BUDGET_TOKENS` on reasoning-enabled Anthropic/Bedrock routes, or state in the boot
    log that the thinking route is unproven there;
  - **timeouts: one shared deadline across every probe request** (loop list, planner list and advisor), not a
    per-request timeout. Worst-case total probe time must fit the ~60 s left by the startup contract (§4 rules,
    "Boot-time budget"): e.g. a 45 s shared deadline, with the advisor (today 60 s alone) inside it. The planner's
    own 5 s is not raised to 60 s. On timeout, log "tool schemas unverified at boot" (nonfatal, as today). If a
    tools-bearing probe cannot fit, the alternative is to run it after the socket binds and gate `/api/ready`
    (`app.py:2174`) on it rather than startup; decide in S0 and state the resulting worst-case boot time here;
  - the `ComposerBootConfigError` text adds owned facts only: `surface`, `tool_count`, and
    `strict_true_count`/`strict_false_count`/`strict_key_omitted` (all 0/0/42 in S0).

Tests (RED first):

- `test_tool_argument_error_category.py`: each producing site maps to its category (parametrised over the sites
  above). `SCHEMA_VALIDATION` splits by `validator` keyword. An unknown category raises at construction.
- Redaction additions (there is no `test_redaction.py`; add to the existing files
  `tests/unit/web/composer/test_tool_redaction_policy.py` and
  `tests/unit/web/sessions/test_tool_invocation_redaction.py`, or create a new
  `tests/unit/web/composer/test_error_code_redaction.py`):
  - the category is persisted;
  - **`error_code` survives `redact_tool_call_response` for one code per option tool** (`plugin_options_invalid`
    on set_source/upsert_node/set_output/set_pipeline, `prompt_template_parts_required` on patch_node_options), while
    an unregistered string in the same field is still redacted. That last case is the negative control. The positive
    cases are RED today (§2.3 probe);
  - error-code registry census: every `error_code=` value passed to `_failure_result` /
    `ValidationEntry(error_code=…)` / `_err(…)` under `web/composer` (excluding `guided/`) is in the registry.
    Mutation control: plant an unregistered code at a producer → red;
  - producer census for `_SAFE_ARG_ERROR_CLASSES`.
- `test_dispatch_arms_characterization.py`: the 3 `"TypeError"` arms and the `MissingRequiredPaths` arm move to
  honest class + category. This is a deliberate pin move.
- Planner rejection codes: every `ToolArgumentErrorCategory` value survives `_closed_planner_rejection_codes`
  unchanged (control: a code outside the closed set still collapses to `validation_error`).
- The two carve-out tools: an extra key sent to each of `request_advisor_hint` and `request_interpretation_review`
  now fails with `schema_shape` before pydantic, and resolving their schema raises no `KeyError`.
- Boot probe:
  - the loop-list probe's kwargs equal `_call_llm`'s kwargs, and the planner-list probe's kwargs equal the pipeline
    planner's, for the same settings (mutation controls: drop reasoning from one side → red; change `max_tokens` on
    the planner-list side → red);
  - the probe sends both tool lists;
  - the total probe budget is pinned: the shared deadline covers every probe request, and its value fits the
    startup contract (control: raise it past the budget → red);
  - BadRequest → `ComposerBootConfigError` naming the surface;
  - timeout → nonfatal.
- MCP session tool definitions have closed roots.

Gates: ruff, mypy, the contracts census, the trust-tier corpus comparison, the attribute/masquerade baselines,
targeted files `-n 0`. Run the testcontainer selection **only if** R3 leads to an epoch bump.

Acceptance:
1. No `error_class` writer names a class that was not raised (§5.1). Instrument: a producer-census test over every
   string-literal `error_class=` keyword and `"error_class":` dict key in `src/elspeth/web/composer` (excluding
   `guided/`), found by AST, not grep. Each literal is either the name of a class that is raised at that site, or
   is on a short, named out-of-scope list with a reason (candidates: the `pipeline_planner.py:2544,2741,2990`
   feedback payload labels, if they stay planner-facing labels rather than audit fields). Mutation control: plant a
   literal `error_class="TypeError"` at a producer → red. The two-string grep in §2.3 is not the instrument: it
   cannot see `MissingRequiredPaths` or `TimeoutError`.
2. A dev session with one forced plugin-option error persists `error_code: plugin_options_invalid`, and one forced
   `prompt_template_parts_required` on patch_node_options persists that code.
3. The census (Appendix A) runs against a fixture DB with known positive and negative rows before it runs on real
   data.
4. Boot succeeds on the dev deployment with the tools-bearing probe, and the logged total probe time is within the
   shared deadline.
5. Collect a baseline over the tutorial canary run and ordinary dev use until the option tools have enough calls to
   compare (see §5.3). Record the numbers in this plan.

**S0a as implemented (error vocabulary, error codes, closed roots): corrections and decisions.** Recorded while
implementing; where this differs from the bullets above, this paragraph is the current state.

- **Deadline sites.** The plan said neither `:1818` nor `:1900` raised anything. Only `:1818` (pre-call deadline) raises
  nothing: its `"error_class"` key is dropped, and `status: COMPOSE_TIMEOUT` is the discriminant. `:1900` is inside
  `except TimeoutError as advisor_exc`, so it now records `type(advisor_exc).__name__`. Both are SUCCESS rows.
- **Honest class where nothing was raised.** The non-object, envelope and required-path gates build a
  `ToolArgumentError` (`tool_batch._pre_dispatch_argument_error`) and record its class and category; the text sent
  to the planner is unchanged. The advisor prompt-budget cap does the same. The advisor validator now returns an
  owned `AdvisorArgumentRejection` (error, class, category) instead of a `dict[str, Any]`.
- **`wire_decode` is not defined in S0.** It has no producer until S2; adding the member then keeps S0 free of a
  category nothing emits.
- **`ToolArgumentError.category`.** Keyword-only. When omitted it is derived: `semantic_rule` with no code, or the
  1:1 category of `DISCOVERY_ONLY` / `DUPLICATE_RESOLVED_INTERPRETATION` / the two rate caps. `SCHEMA_VALIDATION`
  must name `schema_shape` or `schema_bound`. A category that disagrees with its code raises. The 14 pydantic-wrap
  sites pass `model_validation` explicitly, and an AST census keeps new ones from defaulting to `semantic_rule`.
- **`_SAFE_ARG_ERROR_CLASSES`.** It is pinned to the classes that the web ARG_ERROR producers raise, measured by
  triggering each producer with input that can reach it. The result is `IntegerDomainError`, `JSONDecodeError`,
  `JsonBoundaryError`, `ToolArgumentError`, `ValidationError` and `ValueError`. Four classes were removed:
  `MissingRequiredPaths` (the class does not exist), `TypeError` (provider arguments are admitted as `str`, so
  `bounded_json_loads` never sees non-text), and `FloatDomainError` and bare `CanonicalizationError` (the values
  that raise them cannot come out of `bounded_json_loads`).
- **Error-code registry.** The registry is `composer/error_codes.py`. It holds 200 codes: 174 literal codes, the 5
  `ToolArgumentError` codes, every `PluginUnavailableReason` value and every category value. The census reads
  `error_code=` keywords, the fourth positional argument of `ValidationEntry`/`_err`, and `"error_code"` dict keys.
  Non-literal sites (30 of them: forwarders, enum reads, planner feedback projections) are pinned to a reviewed list,
  so a new one fails until it is reviewed.
- **Pipeline planner.** The categories join `_PLANNER_SERVER_REJECTION_CODES`. The discovery `ToolArgumentError`
  rejection codes and both `arg_error_payload_factory` payloads (`pipeline_planner.py` and `pipeline_commit.py`)
  now use the category and `type(exc).__name__`. The planner-facing feedback entries
  (`_allowlisted_argument_error_entry`, `provider_discovery_response._ArgumentErrorResponse`) keep `exc.code or
  "argument_error"`, so S0 changes nothing the planner reads there.
- **R3 reader trace: no epoch bump.** The persisted invocation readers all read by key membership or `.get`:
  `sessions/routes/_helpers.py` (outcomes and pipeline-dispatch recovery), `sessions/service.py`,
  `sessions/guided_audit.py` and `PipelineDispatchAuditBinding.from_persisted_envelope`. The only exact key-set
  reader in `web/` is `_aws_ecs_acceptance/receipt_contracts.py:665`, which is unrelated. The frontend does not
  parse `_redaction_status`/`arg_error` content. `error_category` is additive JSON.
- **Appendix A path.** The ARG_ERROR tool-row content carries `error_category` at the top level (`$.error_category`),
  so the query stands as written.
- **Carve-outs.** The advisor and interpretation-review flat schemas agree with their pydantic models on the
  omission-only fields (`schema_excerpt` and `llm_draft` reject `null` in both; measured). The directional
  `schema_contract` walker covers only `set_pipeline`, so these two tools have the same unproven narrower-than-model
  risk as the other 39 S-gated tools.
- **MCP session tools.** They now advertise `additionalProperties:false`. `server.py` validates only
  `_COMPOSER_TOOL_NAMES` against S, so the session-tool arguments are not held to that closed root at dispatch.
- **`service.py` session_id premise.** `_get_litellm_tools()` filters nothing. `compose()` admits a turn only with
  COMPOSE session authority whose fence names the `session_id`. The `RuntimeError` stays as an invariant, and its
  message and docstring now name the real guard.

**S0b as implemented (provider_served, boot probe): corrections and decisions.** Recorded while implementing;
where this differs from the bullets above, this paragraph is the current state.

- **Verify first: the field arrives.** Loopback recorder on LiteLLM 1.102.0 (lane
  `s0b/provider_served_probe.py`, logs `provider_served_probe.log` and `provider_served_probe_negative.log`): a
  canned OpenRouter-shaped 200 carrying `"provider": "DeepInfraProbe"` reaches `_provider_field_map(response)` on
  both `openrouter/deepseek/deepseek-v4.1-flash` and `openai/…` with a custom `api_base`. Negative control (the key
  removed): absent. Positive control: `model` present in both runs. `_hidden_params.custom_llm_provider` is
  LiteLLM's *routing* provider (`openrouter`/`openai`), not the served endpoint, so it is not used, and no header
  fallback was built. The per-endpoint split in S1 acceptance and Risk 2 therefore stands.
- **`provider_served`.** `ComposerLLMCall.provider_served` is admitted in both `build_llm_call_record` branches:
  the compose loop's admitted-metadata branch (`admit_llm_provider_metadata`) and the pipeline planner's
  `response=` branch, which would otherwise have persisted `None` on exactly the route S1 wants measured. The closed
  shape is 1–64 characters of letters, digits, space, `.`, `_` and `-`, starting and ending alphanumeric; all 38
  `provider_name` values in the three measured endpoint lists fit (longest 14). A present value outside the shape
  (wrong type included) is recorded as `unrecognised`; absence and a blank string are `None`. The contract enforces
  the same shape. The field is in `_LLM_CALL_PUBLIC_AUDIT_FIELDS`; additive JSON in the `llm_call_audit` envelope,
  no epoch bump (its readers read by key).
- **Tool-list builder: zero-argument in S0.** The plan named a function of `(model, api_base, settings)`. In S0
  none of the three has a reader (the list is static until S1's transport resolution), so
  `service.composer_loop_tool_definitions()` takes no arguments and S1 adds them with their first reader.
  `ComposerServiceImpl._get_litellm_tools` is deleted, not kept as a delegate; the 10 test files that named it moved.
- **Request builders.** `service.build_composer_loop_request_kwargs` is used by `_call_llm`, `_call_text_llm` and
  the probe; `pipeline_planner.build_planner_request_kwargs` by `call_model` and the probe. The probe also mirrors
  the Anthropic cache markers each production path applies before building its request.
- **Surfaces.** `loop_tools` (the 42-tool loop list, `max_tokens` 16, discovery effort), `planner_tools`
  (`planner_tool_definitions()`, the planner's `max_tokens` and retry pins, candidate effort) and `advisor`, in that
  order, from `boot_probe.build_composer_probe_requests(settings)`. Production parity is pinned by capturing the
  LiteLLM kwargs of two real `compose()` turns (`tests/integration/web/composer/test_boot_probe_production_parity.py`,
  RED against the pre-S0b probe on assertion, with the reasoning-drop and token-cap mutation controls). Limit: the
  probe sends the default terminal contract, while production sends a request-scoped discovery subset and terminal
  contract, so tool *names* are compared there, not bytes.
- **Anthropic/Bedrock thinking: logged, not raised.** The loop request keeps `max_tokens` 16. When the model is on
  an Anthropic-family route (`supports_anthropic_prompt_cache_markers`), the request carries `reasoning_effort`
  (`openrouter/` gets the `reasoning` object instead, and `azure/` or `vertex_ai/gemini-*` get `reasoning_effort`
  but no thinking-budget cap), and 16 ≤ LiteLLM's
  `ANTHROPIC_MIN_THINKING_BUDGET_TOKENS` (1024, pinned against the installed LiteLLM together with the
  `cap_thinking_budget_to_max_tokens` behaviour), a successful probe logs `composer_boot_probe_thinking_route_unproven`.
  The `planner_tools` request (the planner's 16,384 tokens) exercises the thinking route on the same model and
  endpoint, at candidate effort.
- **Deadline: 45 s shared, probes stay in the lifespan.** One deadline covers all three requests; each planner
  request is also capped at 5 s inside it; the advisor gets the remainder (at least 35 s); a request whose share is
  spent is not sent and is logged `SharedDeadlineExhausted`. Every timeout stays nonfatal and logs that tool
  schemas (or structured-output conformance) were unverified at boot. Worst-case probe time falls from 65 s to 45 s;
  with the OpenRouter catalog prime (5 s connect + 5 s read) it is 55 s, inside the 60 s left by the 150 s startup
  contract, which a test reads from the bicep and ECS runbook. Justification, from read-only census of the archived
  session DBs (instrument controlled with a 1..20 fixture and a wrong-kind row): 23 successful advisor-model calls
  give p95 11.0 s and max 50.8 s (the same values the advisor review reported for its 20 checkpoint-matched calls), so 35 s is about 3× p95 and the 50.8 s outlier becomes an unverified boot; compose-loop calls
  with ≤ 200 completion tokens (11, prompts 56k–104k tokens) p50 1.6 s, max 2.75 s, under the 5 s cap. A rejected
  request returns before generation, so the cap keeps the 400 signal. **Unmeasured:** the `planner_tools` request's
  latency at candidate effort on a "reply ok" prompt; acceptance 4 is its measurement. SSO discovery (up to 15 s per
  request) is outside this split, as it was before S0b.
- **`ComposerBootConfigError`** names the role, model, surface, `tool_count`, `strict_true_count`,
  `strict_false_count` and `strict_key_omitted` (0/0/42 on `loop_tools`, 0/0/20 on `planner_tools`, computed from the
  sent tools) and the existing option-presence flags. Telemetry gains `probed_surface`.
- **In-tree gateway.** Both planner requests are accepted by ELSPETH's own gateway
  (`test_composer_against_gateway.py`, 9 passed), including the terminal schema, which no test had sent through it.

**S0 review fixes (second review round): corrections, decisions and what is still owed.** Recorded after the S0
correctness and gates reviews; where this differs from the two paragraphs above, this paragraph is the current state.
Code in `0be896c48`. Evidence is in the lane under `s0-fix/`. After the fixes the trust-tier corpus is unchanged
against `8ca98eaaf` (2290 findings, 0 added, 0 removed), the soft-mapping census and masquerade baseline are
unchanged, and the affected test set (`tests/unit/web/composer`, `tests/unit/composer_mcp`, `tests/unit/web/test_app.py`
and 4 more files) has one failure, `test_end_advisor_gate_reaches_prompt_template_pipeline_p5_budget_exhaustion`,
which fails identically at `release/0.8.1`.

- **Interpretation-review carve-out, planner-visible change.** Behind the S gate, `request_interpretation_review`
  answers an extra key, a `null` draft or `kind="llm_prompt_template"` with the generic "…, got invalid_schema". At
  `release/0.8.1` the pydantic model answered: an extra key got `validation_errors[].loc`, and the backend-only kind
  got the teaching "surfaced automatically by the backend at turn finalization; do not request it". The generic text
  is what all 40 other S-gated tools already returned at `release/0.8.1` (measured, `f1-parity.log`,
  `f1-parity-base.log`), and the tool description still says "Prompt-template reviews are surfaced automatically by
  the backend; never request one through this tool". The model's `_kind_must_be_requestable` stays: it is the
  model half of S, like every other constraint the flat schema duplicates (lengths, `extra="forbid"`), and two tests
  call the model directly. Restoring a field-level repair signal for every S-gated tool is an S1 item (see S1 files).
- **Dispatch-path pin for that gate.** A compose-loop test sends `request_interpretation_review` an extra key and
  asserts `ToolArgumentError` / `schema_shape` with the handler never awaited. Removing the
  `require_schema_valid_arguments` call turns it red (`model_validation`), `mut-f2-drop-interp-sgate.log`.
- **Canonicalisation reachability.** In the compose loop a non-finite number never reaches canonicalisation: the
  bounded decoder rejects it (`ValueError`, `wire_json_invalid`). The `canonicalization` category is reached only by
  an integer outside the I-JSON range (`IntegerDomainError`), top-level or nested; both arms are now pinned, and
  setting all three sites (or the outcome site alone) to another category turns the pins red. On the MCP sidecar a
  non-finite float does reach `canonical_json` (`ValueError`, `canonicalization`), also pinned. §5.2 is corrected.
- **MCP session tools are now held to their closed roots at dispatch** (`_dispatch_tool`, before the session
  handler), with the same Draft 2020-12 gate, `ToolArgumentError` and shape/bound category as the registry tools
  (`require_arguments_conform_to_schema`, fed a validator compiled and metaschema-checked at import). This closes the
  gap the S0a paragraph recorded ("advertise, not enforce"), as §1.1 requires. Client-visible change: `new_session`
  with a non-string `name` now gets the generic S-gate text instead of the handler's "'name' must be a string";
  the handler's own check stays as the handler boundary.
- **Error-code registry scope.** The census walked only `web/composer`, but plugin-policy findings
  (`validate_composition_state`) and execution validation (`ToolResult.runtime_preflight`) put their codes into
  option-tool responses, and they were still redacted (for example `profile_alias_used_as_bucket` on
  `upsert_node`). The census now walks all of `web/` except named exclusions with reasons (`composer/guided/`, the
  deployment acceptance clients). The registry grows from 200 to 239 codes: 4 plugin-policy codes, 17 execution
  validation codes, the 11 bounded source-proof blockers merged into the authoritative preflight, 2 session-route
  codes, and the 5 inline-blob `<category>_inline_blob_content` codes (from the closed category). 22 new reviewed
  forwarder rows; f-string fragments are no longer read
  as codes. Producers keep their literal codes and are held to the registry by the AST census, not by importing it;
  only `redaction.py` imports `error_codes.py` (the S0 bullet said the producers and `generation.py` would too).
- **Pipeline-commit ARG_ERROR payload** is pinned behaviourally: the persisted `error_code` is the category value and
  `error_class` the raised class. Reverting to `exc.code or "argument_error"` or to the literal `"argument_error"`
  turns it red.
- **Catalog prime deadline.** httpx's connect/read timeouts are per operation, so the OpenRouter catalog prime could
  trickle past the "5 s + 5 s" the S0b budget assumed (reviewer's loopback measurement: 12 s at 1 byte/s, no
  timeout). The prime now runs under a total 10 s deadline (`asyncio.wait_for`; a timeout is a failed prime, logged
  `PrimeDeadlineExceeded`, nonfatal). Measured end to end against a loopback server trickling 1 byte/s for 30 s: the
  boot prime ends at 10.0 s; a fast server still primes (`f3-loopback-drip.log`). The boot-budget test now adds the
  prime's total deadline, not its per-operation timeouts, so the 55 s worst case (10 + 45) is enforced, not assumed.
- **Test-only cleanup.** The four `# type: ignore` comments added in S0b's provider_served test are replaced by a
  typed helper (mypy clean on the file).
- **Acceptance 3 done.** The Appendix A query, read verbatim from this plan, was run read-only against a fixture DB
  written by `persist_compose_turn` with production-redacted rows: exactly one row each for
  `arg_error:schema_shape`, `rejected:plugin_options_invalid`, `ok`, and the two controls under
  `other_failure:cancelled` / `other_failure:plugin_crash` (`appendix-a-fixture.log`). Negative control: an
  unregistered code in the rejection row gives `rejected:<redacted-response-text>` and the check fails
  (`appendix-a-fixture-negative.log`). The script is `s0-fix/appendix_a_fixture.py` in the lane.
- **Still owed before merge:** the full-suite gate (`--stages ruff,mypy,contracts,lints,pytest`), and acceptance 2
  (a dev session persisting `plugin_options_invalid` / `prompt_template_parts_required`), 4 (a dev-deployment boot
  with the logged total probe time, which is also the only measurement of `planner_tools` latency at candidate
  effort) and 5 (the baseline). All three need a dev deployment. The testcontainer selection is not run: no DDL and
  no epoch bump, only additive JSON content.
- **Awaiting John's rulings:**
  - `RejectionRecord.error_code` on ARG_ERROR rows is the failure class (its documented meaning: "first coded
    validation entry, else failure class"), so after S0 the non-object, envelope and required-path rows read
    `ToolArgumentError` where they read `TypeError` / `MissingRequiredPaths`, and the opt-in
    `include_rejection_reasons` messages API loses that distinction. No frontend code reads it (measured: no
    `rejection_reasons` consumer under `web/frontend/src`). Suggested ruling: record `error_category.value` there
    for ARG_ERROR rows (a registered code, like the other rows' codes) and keep the class in `planner_payload`.
  - The advisor's boot allowance drops from 60 s alone to at most 45 s shared, so a call as slow as the observed
    50.8 s outlier becomes a nonfatal, logged "structured-output conformance not verified" boot.
- **No change needed:** the `@trust_boundary` invariant on `redact_arg_error_response` describes the Tier-3 `result`
  parameter; `error_category` is an owned enum argument, not part of that boundary.
- **Soft-mapping census:** S0 moved the pin from 2745 to 2756 (+11: `boot_probe.py` +6, `pipeline_planner.py` +3,
  `service.py` +1, `_dispatch.py` +1). This round adds none (the MCP gate takes a compiled validator, not a mapping).

### S1 — Strict on the 32 mechanical tools (no options decision needed)

Purpose: the first wire change. 32 tools are sent `strict: true` on routes that forward it. The 10 option tools and
the terminal are sent explicit `strict:false`. `none` routes are unchanged.

Files:

- **New `src/elspeth/web/composer/tools/strict_profile.py`:** promote the lane's `check_strict.py` checker. Its
  controls become tests.
- **New `src/elspeth/web/composer/tools/wire_projection.py`:**
  - `project_tool`, `WireTool`, the decode-plan nodes, the ledger, the limits report;
  - `decode_wire_arguments` / `encode_semantic_arguments`;
  - `assert_wire_projection_faithful`;
  - a frozen `_WIRE_TOOL_DEFS` built at import.
- **New `src/elspeth/web/composer/strict_transport.py`:**
  - `resolve_strict_transport(model, api_base, setting) -> ENFORCING | FORWARDING | NONE`, with the rules in §2.2;
  - OpenRouter is detected as in `advisor_request.py` (the `openrouter/` prefix, or an api_base host of
    `openrouter.ai`);
  - **custom endpoints resolve to `NONE` by default.** Any route with a non-OpenRouter api_base
    (`composer_endpoint_base_url` for the planner, the advisor endpoint for the hatch) is `NONE`, whether the model
    name is `openai/…` or bare. A bare model name plus an api_base is how the gateway integration tests and a
    gateway deployment run (LiteLLM resolves it to provider `openai`, §2.2). ELSPETH's own gateway rejects any
    `strict` key with HTTP 400, so under `preferred` a gateway route that sent `strict` would fail boot through the
    new probe and fail every compose call at runtime. A third setting value, `forward_to_endpoint`, opts a custom
    endpoint into `FORWARDING` for an operator who has verified their gateway forwards `strict`;
  - the gateway itself is **not** changed in S1. Adding `strict: bool | None` to `ChatFunctionDef` would force a
    decision on whether the gateway forwards it upstream or honestly drops it; that is a separate gateway-contract
    change (same class as `elspeth-9a46553771` for `reasoning_effort`);
  - `azure/` is `ENFORCING` only when its api-version supports strict (reported as `2024-08-01-preview` or later;
    unmeasured here). Resolve the api-version the route will use; an older or unknown one resolves to `NONE`, and the
    boot probe is the check;
  - Bedrock uses LiteLLM's own `bedrock_converse_supports_strict_tools` only once S5 exists; until then Bedrock is
    `NONE`.
- `src/elspeth/web/config.py`: `composer_strict_tools: Literal["preferred", "forward_to_endpoint", "off"] =
  "preferred"`. `off` forces `NONE` everywhere, which is the remedy when a route rejects `strict`. `required` is
  added only in S4, together with proof.
- `service.py:7719` `_get_litellm_tools`:
  - becomes a lookup over `_WIRE_TOOL_DEFS` for the resolved dialect, stamping `function.strict`;
  - `strict` is `true` only when the tool is strict-capable, the transport is not `NONE` and the setting is not
    `off`; it is explicit `false` on the other tools of a non-`NONE` transport;
  - it is omitted on `NONE`;
  - `apply_anthropic_cache_markers` (`llm_response_parsing.py:768`) still marks the last tool (`wire_secret_ref`).
- `tool_batch.py`:
  - call `decode_wire_arguments` right after the non-object check; delete the inline envelope block
    (`:961-1008`), whose `wire_envelope` emission (added in S0) moves into decode's `ENVELOPE_UNWRAP` node;
  - `_replace_llm_tool_call_arguments` (`:433`) calls `encode_semantic_arguments`;
  - `ComposerToolInvocation` gains `strict_sent: bool | None` and `wire_conformant: bool | None`, with honest
    `None` where no arguments were decoded.
- **S-gate repair signal (carried from the S0 review).** Every S-gate rejection reaches the planner as the bare
  "must be object conforming to …, got invalid_schema", with no `validation_errors`: the `ToolArgumentError`
  projection drops the jsonschema summary, so the planner is not told which field failed. This was true of all 40
  S-gated tools at `release/0.8.1` (measured, lane `s0-fix/f1-parity-base.log`), and S0 put the two carve-outs and
  the 6 MCP session tools behind the same gate. Give the S-gate error a closed loc (the failing `absolute_path`
  projected through the tool schema's own property names, and the offending key for `additionalProperties`) and emit
  it as `validation_errors`, as `arg_error_payload` does for pydantic causes. This changes planner-visible tool
  results on every tool, so it lands with S1 and after the S0 baseline, not inside it.
- `pipeline_planner.py:1504` `planner_tool_definitions`: the discovery subset comes from `_WIRE_TOOL_DEFS`; the
  terminal (`:1481`) is sent `strict:false` where the dialect carries the key. Decode also runs for planner
  discovery calls.
- **Escape-hatch route.** The pipeline planner sends its terminal to a second route within one request: on a hatch
  turn `call_model` uses `model_override=model_config.escape_hatch_model` with
  `tools_override=[planner_terminal_tool_definition(...)]` (`pipeline_planner.py:4225-4226`), where the service
  passes `escape_hatch_model=composer_advisor_model` and `escape_hatch_api_base=self._advisor_endpoint_base_url`
  (`service.py:4357/4362`, `4769/4774`, `5276/5281`). The deployed hatch is `openrouter/z-ai/glm-5.3`; the default
  advisor model is native `anthropic/`, whose route is `NONE`. So:
  - resolve transport and dialect **per `effective_model` and effective api_base**, at the point where cache
    markers are already chosen per effective model (`pipeline_planner.py:3820-3827`,
    `supports_anthropic_prompt_cache_markers(effective_model)`), and stamp the tools of that call from that result;
  - compute `strict_sent` and `wire_conformant` against the W actually sent on that call, and record the dialect on
    that call's `ComposerLLMCall`;
  - in S1 the hatch carries only the non-strict terminal, so its wire change is at most an explicit `strict:false`
    on a `FORWARDING`/`ENFORCING` hatch route and nothing on a `NONE` one. S3 must not send a strict terminal to a
    hatch route that no boot probe has exercised with tools (see S3).
- `ComposerLLMCall`: add `tool_contract_dialect` (closed) and `strict_tool_count: int | None`. The existing
  `tools_spec_hash` already covers the bytes. Both new fields go into `composer/audit.py`
  `_LLM_CALL_PUBLIC_AUDIT_FIELDS` in the same commit, each with a persisted-projection test and a remove-the-key
  mutation control (§2.4: the whitelist silently drops anything else).
- `/api/system/status` (`app.py:2198`) shows `composer_tool_contract: {setting, transport, dialect,
  strict_tools: "32/42", boot_acceptance}`, with closed values only.
- Boot probe: it now sends the stamped lists, and its error text carries the real strict counts. When the hatch
  route resolves to a non-`NONE` transport, the probe also sends the stamped terminal to the hatch route, inside the
  same shared deadline (S0). The existing advisor probe sends no tools, so it does not cover this.

Tests (RED first):

- `test_strict_profile.py`: the checker's controls (2 negatives give 0 rows, 11 positives fire, 1 mutation goes red).
- `test_wire_projection.py`:
  - partition pinned at 32/10, and the decode surface pinned to 13 promoted / 11 omission-only (§2.1): 13
    `STRIP_NULL` positions, of which the 11 in §2.1 are the ones where the flat schema rejects null;
  - no strict-capable description instructs omission of a wire-required key (control: plant "Omit X" → red);
  - the enum rule: no `null` inside any `enum` anywhere in W (control: plant one → red);
  - fail-closed on an unknown keyword (inject one → import error);
  - the ledger renders into descriptions, and every ledger entry sits on an S-gated tool;
  - the limits report fails closed (control: lower one limit below the corpus → red);
  - the directional walker pointed at W goes red (pinned negative control);
  - `_WIRE_TOOL_DEFS` is identical when built under two plugin allowlists (coupling control).
- `test_wire_decode.py`:
  - null → absent only at promoted positions;
  - an omitted wire-required key passes through, is admitted by S, and gives `wire_conformant=False`;
  - a null inside a patch map is untouched (control: a decode that strips it turns the equivalence property red);
  - a decode that *rejects* on W failure turns the test red;
  - round trip `decode(encode(s)) == s` over the existing emission fixtures, plus hypothesis over the wire subset;
  - set_pipeline `{source, sources:null}` is accepted via the web wire and rejected via flat.
  - Envelope via decode: a valid enveloped set_pipeline call gives `wire_conformant=True`. Control: its unwrapped
    form, validated against the same W, is `False`. `{}`, `{"pipeline": 1}` and `{"pipeline": {…}, "x": 1}` each
    give `wire_envelope`, with the transcript rewrite and redacted audit arguments as today.
- `test_strict_transport.py`: the prefix × api_base × setting matrix → transport, including a bare model name
  (`gpt-5.5`) plus a custom api_base → `NONE` under `preferred` and `FORWARDING` under `forward_to_endpoint`, and the
  per-tool stamping (true / explicit false / omitted).
- Escape hatch: a planner run where the planner route and the hatch route resolve differently (e.g. planner
  `openrouter/…` = `FORWARDING`, hatch `anthropic/…` = `NONE`). The hatch call carries no `strict` key, and its
  `strict_sent`/`wire_conformant`/`tool_contract_dialect` are computed against the hatch's W, not the planner's.
- `test_wire_fidelity_matrix.py`: promotes `wire_probe.py`/`real_tools_probe.py` (loopback recorder, no network
  beyond localhost). For each route in §2.2 it asserts the transmitted `strict` and schema, **including the
  known-negative rows**:
  - `anthropic/` drops `strict`;
  - Bedrock strips `null` from a synthetic `enum:[…,null]`;
  - Azure flattens a root `oneOf`;
  - a gateway row against the real in-tree gateway: a request carrying `function.strict` is rejected with 400
    (known-negative), and the `NONE`-stamped list under `preferred` is accepted.

  A LiteLLM bump that changes an adapter turns this red. It extends
  `test_tool_schema_contract._provider_transported_set_pipeline_schema`.
- `test_provider_cache_markers.py`: `cache_control` and `function.strict` coexist on the last tool.
- `test_tool_declarations.py` must **not** move (S unchanged). If it moves, that is scope creep.

Gates: as S0, plus the soft-mapping census re-pin for the two new modules, `generate_skill_inventory.py --check`
(unchanged: no tool is added or renamed), and `tests/integration/web/composer/test_composer_against_gateway.py`
(including `test_boot_probe_succeeds_against_gateway` and the tool round trip), which must stay green unchanged.

Boot, routing and request shape:

- never `parallel_tool_calls`, never `tool_choice`, and no `provider.require_parameters` on the planner (the
  advisor keeps it);
- one explicit `false`/`true` per tool on non-`NONE` transports.

Acceptance:
1. The boot probe's `strict:true` list on 32 tools is accepted **by the endpoint that served the probe** on the dev
   deployment (OpenRouter deepseek; record its `provider_served`), and on one Azure or OpenAI deployment if one is
   available (R8). On OpenRouter one boot exercises one of the 24 routed endpoints, so a pass does not prove the
   others accept `strict`; a 400 from another endpoint shows up only at runtime, as `_BadRequestLLMError`. Where the
   hatch route resolves to a non-`NONE` transport, the stamped terminal is accepted by the hatch endpoint too (record
   its `provider_served`).
2. The fidelity matrix is green, including its known-negative rows.
3. After a comparable window, compare against the S0 baseline:
   - runtime `_BadRequestLLMError` counts per `provider_served`;
   - `wire_conformant=False` rate per tool on the 32, split by `provider_served`;
   - ARG_ERROR categories on the 32;
   - first-call latency.

   State the result plainly. **S1 may change nothing measurable**, because the 32 tools rarely fail. The value of
   S1 is the contract plus the per-endpoint conformance evidence it produces.

### S2 — Options carrier and structure maps (blocked on R1 and R2)

Starts only if John rules R1 in, using the S0/S1 data. Content:

- The 13 option/patch positions become `options_json`/`patch_json` strings, nullable where the flat field is
  optional.
  - Decode: `bounded_json_loads` under one per-call `JsonTraversalBudget`.
  - It runs **before** audit canonicalisation, so option values that are `Sensitive` are never persisted as an
    opaque string.
- The 5 structure maps become closed pair arrays (`[{label,target}]`, `[{branch,input}]`, `[{name,…}]`). Decode is
  1:1 and **rejects duplicate keys** (`wire_decode`).
  - `branches` is `list[str] | dict[str,str]` in S (`redaction.py:1946`), so W carries it as a nested `anyOf` of the
    two array forms.
- set_pipeline `source`/`sources` become required and nullable, and the nested `oneOf` goes to the ledger.
- The tri-state detector gate: no field inside decoded options is treated as null→absent. Before S2 lands, prove
  there is no promoted field where null means something different from absent.
- Error pointers are re-encoded from semantic paths to wire paths for pairs and carriers.
- All 42 loop tools become `strict:true`.
- Blast radius, all of which must move in the same slice:
  - `_TOOL_ARGUMENT_JSON_TYPE_GUIDANCE` (one 134-character sentence, not a per-field table) and
    `test_tool_argument_type_guidance_gate.py` (18 test functions, 25 collected items); the carrier positions become
    exempt by name;
  - `test_tool_knob_teaching_gate.py`;
  - `composer_wire_census.py`/`composer_teaching.py`;
  - `skills/pipeline_composer.md` tool-argument examples;
  - `planner_authoring_aids` `inline_form.example_options`;
  - **`src/elspeth/web/execution/_validation_diagnostics.py:135-168,368-408`**, which renders literal
    `patch_node_options(... patch={...})` and `patch_source_options(...)` repair calls;
  - `src/elspeth/web/interpretation_state.py:614` and `src/elspeth/web/execution/service.py:3386`, whose comments
    and docstrings name the patch tools.
- Before S2, verify whether a length cap applies to the raw `function.arguments` string upstream of
  `bounded_json_loads` (envelope-first O6).
- **Acceptance gate:** the fixed-script tutorial canary (ADR-031, ADR-049; same backend, used purely as an
  instrument) plus the dev-session census, compared with the S1 window:
  - per option tool: `wire_decode` + `plugin_options_invalid` + ARG_ERROR rate;
  - planner turns until an accepted pipeline.

  If either measure regresses beyond John's threshold, revert the whole S2 wire change.
  - The tools that carry options cannot be strict without the carrier. Pair maps and nullable source/sources
    would then be churn in the planner-facing shape with no grammar gained.
  - This differs from envelope-first, which proposed keeping the maps.

Tests (RED first):
- `test_wire_decode.py` additions:
  - a duplicate pair key → `wire_decode`;
  - carrier text that is not JSON, or not an object → `wire_decode`;
  - carriers in one call share one traversal budget (control: two carriers each under the cap but over it together
    → `wire_json_bounds`);
  - `{"k": null}` inside `patch_json` survives decode and deletes the key through `_apply_merge_patch`;
  - pair-map round trip, including `branches` in both list and map form.
- Decode runs before canonicalisation: a `Sensitive` option value inside `options_json` never appears in
  `arguments_canonical` as carrier text (control: skipping decode makes it appear → red).
- The partition moves to 42/0. The decode surface is pinned to 87 promoted / 28 omission-only.
- Tri-state detector: a synthetic promoted field whose null differs from absent turns the gate red.
- Error pointers name the wire path (`options_json`, `routes[2].target`) for decode failures, and the semantic path
  (`options.<key>`) for content failures.

Gates: as S1, plus the deliberate pin moves in the blast-radius list above, and `generate_skill_inventory.py
--check` if any teaching surface changes. Run the full-suite gate before merge: this changes the teaching and
planner-facing surface.

### S3 — Pipeline-planner terminal (after S2)

`emit_pipeline_proposal` is projected by the same compiler: the set_pipeline W plus `claimed_deferred_intent_ids`,
promoted and nullable, with `uniqueItems` moved to the ledger.
- The terminal's `not`, `propertyNames` and `maxLength` constraints go to the ledger.
- A `pattern` is kept only where it passes the allowlist rule; any other goes to the ledger.
- `Draft202012Validator(terminal_contract.schema)` (`pipeline_planner.py:4414`) keeps enforcing all of them.

Files:
- `pipeline_planner.py`: `planner_terminal_tool_definition` (`:1481`) projects through `wire_projection` for the
  dialect of the route the call goes to (planner or hatch, S1); the terminal payload is decoded before
  `_PlannerTerminalPayload`.
- `_assert_planner_call_matches_manifest` keeps comparing `tools_spec_hash`.
- **`capability_skill.py`.** The capability-manifest builder requires the advertised terminal's `pipeline`
  sub-schema to hash-equal the canonical set_pipeline schema (`capability_skill.py:244-248`:
  `if stable_hash(advertised_schema) != stable_hash(canonical_schema): raise AuditIntegrityError("planner terminal
  does not advertise the canonical pipeline schema")`), and binds `canonical_schema_hash=stable_hash(advertised_schema)`
  (`:261`). S3's projection changes that sub-schema on purpose (promoted nullable fields; `not`, `propertyNames`,
  `maxLength`, `uniqueItems` moved to the ledger), so as written every planner call would raise. S1 does not trip
  it, because adding `function.strict` leaves `parameters` unchanged. Change the check to compare the advertised
  terminal against `project_tool(canonical)` for the dialect actually sent, and bind `canonical_schema_hash` to S
  (the pre-projection canonical schema) plus the dialect id.
- Hatch route: the terminal is sent `strict:true` only on a route that the boot probe exercised with the stamped
  terminal (S1 boot-probe bullet). Otherwise the hatch keeps the `none` dialect for the terminal. The deployed hatch
  (`openrouter/z-ai/glm-5.3`) and the default (`anthropic/`, `NONE`) differ, so this is decided per deployment.

Tests (RED first):
- RED today: the manifest check raises `AuditIntegrityError` on a projected terminal; after the change it accepts
  `project_tool(canonical)` for the sent dialect and still rejects a terminal that differs from that projection.
- The terminal passes `strict_profile`.
- The ledger covers every dropped terminal keyword, and each one is still enforced by the terminal validator.
  Control: a name that violates a ledgered `not` is still rejected, with its existing structural feedback.
- A `claimed_deferred_intent_ids: null` on the wire decodes to the model default.

Gates: as S2.

Acceptance: compare the planner `rejection_codes` distribution and the first-turn success rate on an empty state
with the S1 window.

If R1 is refused, the terminal stays `strict:false` permanently. That is stated in §6.

### S4 — Enforcement proof on the deployed route (optional; blocked on a measurement and on R5)

The aim is to be able to *claim* that strict is enforced, not just that it was sent. Add:

- a `required` value for `composer_strict_tools`;
- a boot-only canary tool, never in any compose list: a schema-versus-instruction conflict, run as a test request
  (strict) and a control request (non-strict);
- OpenRouter endpoint pinning (`provider: {only: [...], allow_fallbacks: false}`), proven per endpoint;
- a runtime breach monitor: `wire_conformant=False` on a strict tool under a proven route → counter plus a status
  downgrade, never an admission change.

**Prerequisites, measured before any code:**

1. Run the canary pair against deepseek-v4.1-flash on each candidate endpoint, and against gpt-5.5, a few times
   each. If the control run conforms reliably, the instrument cannot discriminate, and S4 is not worth building.
2. Verify that a caller-owned LiteLLM `Logging` object captures the transformed body of one `acompletion` call.
   **Never** use the `callbacks=` kwarg: `litellm/utils.py:912-931` appends it to the global callback lists. Never
   use `return_raw_request`, which sends a real request with a fake key. If capture cannot be done per call, the CI
   fidelity matrix remains the only adapter evidence.

### S5 — Anthropic-shaped dialect (optional; R6)

`anthropic_strict`:
- optional properties stay optional, and `null` is removed from their types;
- `minimum`, `maximum`, `minLength` and `maxLength` go to the ledger;
- `minItems` is kept only when it is 0 or 1;
- a static packer chooses the ≤20 strict tools under the 24-optional and 16-union caps (measured today: 20 selected,
  13 optional, 0 union);
- it applies to unflagged `bedrock/anthropic.*` routes, and to `openrouter/anthropic/*` if forwarding the
  `x-anthropic-beta` header is verified.

set_pipeline and upsert_node stay non-strict under these caps unless S itself gets fewer optional properties
(Anthropic's own advice: make fields required when a value always exists).

---

## 5. Error vocabulary

### 5.1 Honest labels

- `error_class` records the exception class that was actually raised: `ToolArgumentError`, `JSONDecodeError`,
  `JsonBoundaryError`, `CanonicalizationError`, and so on. No site writes a class name that was not raised. This is
  gated by the S0 producer-census test over every string-literal `error_class` writer (S0 acceptance 1), not by a
  grep for two names.
- The 6 hand-label sites in §2.3 construct a `ToolArgumentError` with a category, or return a category directly in
  the advisor's case. The other literal labels in §2.3 (`MissingRequiredPaths`, the deadline `TimeoutError`s, the
  planner feedback labels) are fixed in S0 or named on the census's out-of-scope list with a reason.
- `PLUGIN_CRASH` rows keep `type(exc).__name__`, as today.

### 5.2 Closed category `ToolArgumentErrorCategory`

It is value-free, and it is persisted on every ARG_ERROR alongside `field_count`/`validation_error_count`.

| category | stage | produced at |
|---|---|---|
| `wire_json_invalid` | wire | `bounded_json_loads` JSONDecodeError, or ValueError for a non-finite constant (`NaN`, `Infinity`, an overflowing float) |
| `wire_json_bounds` | wire | `JsonBoundaryError` |
| `wire_not_object` | wire | `tool_batch.py:929` (was `"TypeError"`) |
| `wire_envelope` | wire | `tool_batch.py:967/987/994` in S0 (was `"TypeError"`); from S1, decode's `ENVELOPE_UNWRAP` node |
| `wire_decode` | wire | S2 and later only: duplicate pair key, or carrier text that is not JSON or not an object |
| `canonicalization` | semantic | compose loop: `IntegerDomainError`, an integer outside the I-JSON range (non-finite numbers never get past the decoder); MCP sidecar: `canonical_json`'s `ValueError` on a non-finite float, which the MCP SDK's decoder admits |
| `missing_required_path` | semantic | required-paths walker (was `MissingRequiredPaths`) |
| `schema_shape` | semantic | S Draft 2020-12 failure whose `validator` keyword **is** in `WIRE_KEYWORD_ALLOWLIST`, so a grammar should have prevented it |
| `schema_bound` | semantic | S failure whose keyword is **not** on the allowlist (`minLength`, `maxLength`, `not`, `oneOf`, `uniqueItems`, …), i.e. a ledgered constraint |
| `model_validation` | semantic | pydantic rejection after S passed (cross-field rules, the presence XOR); also the advisor schema failure (was `service.py:7841`) |
| `prompt_budget` | value | advisor prompt-size cap (was `service.py:7860` `"ValueError"`) |
| `discovery_only`, `duplicate_resolved_interpretation`, `rate_cap_per_session_day`, `rate_cap_per_term` | value | the existing `ToolArgumentError.code` values, carried 1:1 |
| `semantic_rule` | value | handler `ToolArgumentError` with no code |

- `wire_conformant` is **not** a category. It is a separate per-invocation flag, recorded on every call, including
  admitted ones.
- **SUCCESS-status rejections** are counted by `error_code`, which S0 makes survive redaction. They are not
  reclassified as ARG_ERROR: their semantics stay as they are (`ComposerToolStatus` docstring,
  `contracts/composer_audit.py:52-60`).
- The pipeline planner's `rejection_codes` use the same category values. The result is one vocabulary across both
  planner surfaces and the MCP sidecar.

### 5.3 Measurement before and after

The census is Appendix A. It is keyed on `(tool, status, error_category | error_code, strict_sent,
wire_conformant, provider_served)`.

| When | What to read | Decision it feeds |
|---|---|---|
| After S0 | Per option tool: `schema_shape` + `model_validation` + `wire_*`, which a grammar or carrier could affect, versus `schema_bound` + `semantic_rule` + `plugin_options_invalid` + uncoded rejections, which it could not | R1 |
| After S1 | On the 32: the `wire_conformant=False` rate per `provider_served` (a high rate on endpoints that advertise structured outputs means strict is being ignored); change in `schema_shape`; first-call latency | S4 worth building; R5 |
| After S2 | The acceptance gate in §4 S2 | Keep or revert the carrier |

Sample size: 151 calls with 4 errors is not enough to call any direction. Name the window by calls per option tool,
not by days. My suggestion is at least 200 calls per option tool, or John sets a number.

---

## 6. Risks, provider caveats and open decisions

### 6.1 Risks

1. **S1 may change nothing measurable.** The 32 tools rarely fail, and 2 of the 4 archived errors were on
   set_pipeline, which stays non-strict until R1. Mitigation: S0 comes first, and §5.3 says what to read.
2. **Silent non-enforcement on the deployed route.** 10 of the 24 deepseek endpoints do not advertise structured
   outputs, including the first-party one, and `require_parameters` does not select on tool-level strict. The boot
   probe detects rejection, not ignoring. `wire_conformant` split by `provider_served` is the detector. Claiming
   enforcement needs S4.
3. **The Responses bridge with an explicit `false`.** Today's omitted key arrives as `strict:null` and may be
   normalised by OpenAI. S1 sends `true` on the 32 and `false` on the 10.
   - Open objects cannot be put into strict form, so the 10 should not have been strict-normalised before. By
     reasoning, then, explicit `false` loses nothing; the actual behaviour is unmeasured.
   - The boot probe proves only that an explicit `false` is *accepted*. Its behaviour needs a live call (R8).
4. **Azure strict with parallel calls.** Microsoft says to set `parallel_tool_calls=false` with strict. The planner
   dispatches up to 16 calls per turn, and serialising them would add round trips on the tutorial chokepoint. The
   plan never sends the parameter. If Azure rejects or degrades strict with parallel calls, `composer_strict_tools=off`
   on that deployment is the remedy until it is measured.
5. **Keywords kept on the wire (`pattern`, `minimum`, `maxItems`, `format:uuid`)** may be rejected by a particular
   model or gateway. The boot probe turns that into a boot failure naming the surface. The remedy is `off`, or
   removing the keyword from the allowlist.
6. **First-use grammar compile latency** on strict routes (Anthropic compiles are cached for 24 h; OpenAI's are
   similar). The tools-bearing boot probe may warm the cache on the planner route, but only within the shared probe
   deadline. Watch first-call latency in S1.
7. **LiteLLM upgrades** can change adapter behaviour. The fidelity matrix is pinned to the locked version and goes
   red on change.
8. **Epoch interaction.** The release branch is already at epoch 66. If R3 leads to a bump, fold it into any bump
   already owed and follow the house deploy note (rename the session DB, `bootstrap-admin`).
9. **Boot duration.** The probes run before uvicorn binds, so their total time counts against the 150 s startup
   contract on ACA and ECS (§4 rules, "Boot-time budget"). A cold database plus a slow or hung provider, which is
   exactly the case the nonfatal timeout exists for, must not become a restart loop. The shared deadline in S0 is the
   mitigation; the fallback is to gate `/api/ready` instead of startup.
10. **Custom gateways that reject `strict`.** ELSPETH's own gateway rejects the key with HTTP 400 (§2.2). Custom
    endpoints therefore default to `NONE`, and forwarding is an operator opt-in (`forward_to_endpoint`).

### 6.2 Provider caveats (summary)

- **Native `anthropic/`:** `strict` is silently dropped by LiteLLM 1.102 and cannot be enforced. S is the contract.
- **Bedrock:** `None` values are stripped, so the `none` dialect keeps today's bytes. Strict comes only with S5.
- **OpenRouter:** enforcement differs per endpoint. For Anthropic models, strict is stripped without a beta header.
- **Custom gateways:** ELSPETH's own gateway rejects `strict` (HTTP 400, measured). Other gateways are unknown. So
  any non-OpenRouter api_base, with an `openai/` or a bare model name, resolves to `NONE` (today's bytes) unless the
  operator sets `forward_to_endpoint`, and then the boot probe decides.
- **Escape hatch:** the pipeline planner's hatch route (advisor model + advisor endpoint) is resolved on its own
  (S1), and it gets a strict terminal only after a tools-bearing probe of that route (S3).

### 6.3 Open decisions for John

1. **R1 — options carrier.** Should the 13 option/patch positions become `options_json`/`patch_json` JSON text,
   together with pair maps? This reverses the "not strings containing JSON" guidance for those named fields only.
   - *Recommendation:* decide after the S0 baseline. Take the carrier only if shape errors dominate on the option
     tools, and only behind the S2 canary gate.
   - If it is refused, the 10 tools and the terminal stay `strict:false` permanently, with S as their contract. Say
     so plainly in the release notes: "every tool strict" is then met server-side for those 10, not by grammar.
2. **R2 — map transcoding.** Is pair-array ↔ map transcoding at the web wire boundary representation, not server
   authoring, under the composer invariants?
   - *Recommendation:* yes. It is lossless and 1:1, it rejects duplicates, and it chooses nothing.
3. **R3 — `error_class` and the epoch.**
   - Option (a): keep `error_class` as the honest exception class and add `error_category`. The keys are additive
     JSON, so no DDL is needed, provided S0 confirms no reader rejects unknown keys.
   - Option (b): redefine `error_class` as the category, which gives one field but a status-dependent meaning, plus
     an epoch bump.
   - *Recommendation:* (a). The hand-labels disappear either way, and a bump is taken only if the S0 reader trace
     finds a closed reader.
4. **R4 — setting.** Should `composer_strict_tools: preferred|forward_to_endpoint|off` default to `preferred`
   (which leaves custom endpoints on `NONE`)?
   - *Recommendation:* yes. `required` arrives only with S4's proof.
5. **R5 — the deployed OpenRouter route.** Run `preferred` unpinned, or pin to structured-output endpoints once S4
   exists?
   - *Recommendation:* run unpinned in S1, and decide on pinning from the per-endpoint `wire_conformant` data.
6. **R6 — the Anthropic dialect (S5).** Worth building for unflagged Bedrock Claude?
   - *Recommendation:* not now. Revisit if a Bedrock deployment becomes a priority.
7. **R7 — `set_source_from_blob` MIME-based plugin inference.** This is flagged only, not ruled on. Arguably the
   server chooses a component there. A strict typed branch would force the planner to choose instead. It is not
   needed for any slice here.
8. **R8 — a probe deployment.** Is there an Azure or OpenAI deployment for S1 acceptance? Two things need a live
   call there: explicit `strict:false`/`true` on the Responses bridge, and Azure strict with parallel calls. Without
   one, those rows stay "accepted at boot, behaviour unmeasured".

---

## 7. Out of scope

- **Guided-only tools and surfaces** (guided mode is being removed):
  - `guided/chat_solver.py` `resolve_source`, `reselect_source_plugin`, `retain_deferred_intent`,
    `manage_deferred_intent`, `resolve_sink`;
  - `_dispatch.get_discovery_tool_definitions` (`:369`);
  - the guided `plan_pipeline` call sites and their request-selected terminal contracts.

  They are not on the freeform wire, and investing in them contradicts the removal.
- **The advisor.** It is already strict through `response_format` (`advisor_output.py`, `advisor_request.py`) and
  has its own probe.
- **MCP provider strictness.** MCP has no provider in the loop, so it keeps the flat S. It does inherit honest
  categories and closed session-tool roots in S0. MCP's up-to-3× revalidation per call is a performance tidy-up,
  not a contract change.
- **Per-principal or per-plugin option schemas in tool definitions.** Rejected in §1.5. The key-classification table
  and tri-state detector from the per-plugin lane remain written groundwork.
- **Patching LiteLLM** so that native `anthropic/` forwards `strict`. That is an upstream change.
- **`tool_choice` and `parallel_tool_calls` in the compose loop.** Both change planner behaviour. Anthropic rejects
  a forced `tool_choice` when extended thinking is on, and planner reasoning is on by default
  (`composer_discovery_reasoning_effort="low"`, `composer_candidate_reasoning_effort="high"`, `config.py:270-271`);
  an operator can set `none`, which sends no hint (`reasoning.py:30-40`).
- **Retiring `patch_*` tools.** That is orthogonal. It forces new handler behaviour, which is carrying server-owned
  keys across replacement, and it breaks the `prompt_template_parts_required` guard (`transforms.py:1655-1670`).
- **Generating S from the pydantic models.** A worthwhile follow-on, but it moves the `test_tool_declarations.py`
  pins (93 collected tests, most of them byte-identity pins) and does not depend on provider strictness.

---

## Appendix A — tool outcome census (SQLite session DB, read-only)

Control it before trusting it. Build a fixture DB through the normal writer containing:
- one ARG_ERROR row;
- one SUCCESS row with `success:false` and `error_code`;
- one plain SUCCESS row;
- negative controls: one CANCELLED row and one PLUGIN_CRASH row. Both also carry `error_class`
  (`src/elspeth/web/sessions/routes/_helpers.py:586-592`, `redact_failure_response`), and neither may be counted as `arg_error`.

The query must return exactly one of each, with the two controls under `other_failure:`. Then run it read-only against a copy of a dev session DB. Never run it
against the served deployment's live file.

```sql
WITH calls AS (
  SELECT json_extract(tc.value, '$.id')            AS call_id,
         json_extract(tc.value, '$.function.name') AS tool
  FROM chat_messages m, json_each(m.tool_calls) tc
  WHERE m.role = 'assistant' AND m.tool_calls IS NOT NULL
)
SELECT c.tool,
       CASE
         WHEN json_extract(t.content, '$._redaction_status') = 'arg_error'
           THEN 'arg_error:' || coalesce(json_extract(t.content, '$.error_category'), json_extract(t.content, '$.error_class'))
         WHEN json_extract(t.content, '$.error_class') IS NOT NULL
           THEN 'other_failure:' || coalesce(json_extract(t.content, '$._redaction_status'), 'unknown')
         WHEN json_extract(t.content, '$.success') = 0
           THEN 'rejected:' || coalesce(json_extract(t.content, '$.validation.errors[0].error_code'), 'uncoded')
         ELSE 'ok'
       END AS outcome,
       count(*) AS n
FROM chat_messages t
JOIN calls c ON c.call_id = t.tool_call_id
WHERE t.role = 'tool' AND json_valid(t.content)
GROUP BY 1, 2
ORDER BY 1, 2;
```

- Not yet verified: which JSON path the persisted ARG_ERROR content uses for `error_category`. S0 decides that path,
  so update the query in the same commit.
- `strict_sent`, `wire_conformant` and `provider_served` are read from the invocation and LLM-call envelopes once
  S0/S1 add them.
- Before S0, the `rejected:` arm returns `<redacted-response-text>` (§2.3).
