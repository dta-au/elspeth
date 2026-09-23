# Composer strict tool contracts — S1 implementation plan

- **Date:** 2026-09-23
- **Master plan:** `docs/plans/2026-09-23-composer-strict-tool-contracts.md`. It holds the design; this file holds the
  steps. This plan does not change the design. Where the live code disagrees with the master plan, §3 lists the
  correction and its evidence, and the tasks follow the correction.
- **Branch:** `feat/strict-tool-contracts-s1`, based on local `release/0.8.1` at **`85ebf2739`**. That commit already
  contains S0 (S0a, S0b, the S0 review fixes and the Codex review fixes).
- **Paths:** `$W` is the worktree root (`git rev-parse --show-toplevel`). `$L` is `$W/.claude/lanes/s1`, the lane
  directory (gitignored). It holds the reader notes (`understand-{tools,transport,dispatch,gates}.md`) and every
  instrument and log cited below. `$OLD` is the design lane, `.claude/lanes/strict-tool-contracts` inside the
  `strict-tool-contracts` worktree (read only; it holds the S0 evidence, e.g. `s0-fix/appendix_a_fixture.py`). Do not
  write `$L`, `$OLD` or any user-home path into a tracked file.
- **Citations:** every `path:line` below was measured in `$W` at `85ebf2739`. They will drift as tasks land, so
  re-anchor each one with `grep -n` before you edit. **M** means measured by running code (the log is in `$L`),
  **R** means read from source, and **I** means inferred and not run.

---

## 1. Header

### 1.1 Goal

This is the first change to the tool wire. On every route that forwards `strict`, the compose loop and the
pipeline-planner discovery subset send the 32 mechanical tools with `strict: true`. On those same routes, the 10
option-bearing tools and the planner terminal `emit_pipeline_proposal` are sent with an explicit `strict: false`.
Routes that cannot carry `strict` send the same bytes they send today. Every call is then classified against the
wire schema W it was sent under (`strict_sent`, `wire_conformant`), and every LLM call records its dialect and its
strict-tool count. S stays the only thing that admits or rejects a call.

Under the default setting (`preferred`) the wire change reaches **OpenRouter routes only**: hosted OpenAI and Azure
resolve to `NONE` under `preferred` until a live measurement (R8) and are `ENFORCING` only under
`forward_to_endpoint` (§7.3 ruling 7). The shipped default config (bare `gpt-5.5` planner, `anthropic/` advisor) therefore
keeps today's bytes on both routes.

### 1.2 Adopted decisions (John's recommendations R4, R5 and R6, adopted for S1; each can be reversed by config)

- **R4.** `composer_strict_tools: Literal["preferred", "forward_to_endpoint", "off"]`, default `"preferred"`.
  Under `preferred`, custom endpoints resolve to `NONE`. Setting `off` forces `NONE` on every route, which restores
  today's bytes everywhere. That is the operator's remedy if a route rejects `strict`. **Ruled (lead, provisional;
  John may overrule), §7.3 item 7:** under `preferred`, hosted OpenAI and Azure also resolve to `NONE` until R8 has
  measured them; `forward_to_endpoint` is the opt-in that makes them `ENFORCING`.
- **R5.** The deployed OpenRouter route runs unpinned. There is no `provider.only`, no `require_parameters` on the
  planner and no endpoint pinning.
- **R6.** There is no Anthropic-shaped dialect. Every Anthropic-family route resolves to `NONE`.
- **S2 is out of scope.** No options carrier and no structure maps. The 10 option tools and the planner terminal
  are sent with an explicit `strict:false` on non-`NONE` transports, and S remains their contract.

### 1.3 Decisions this plan takes where the reader notes left a question open

Each decision below was needed to make the plan executable. Each is reversible, and each is listed again in §7.3
so John can overrule it.

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Where do the compose-loop wire facts persist? The loop never writes invocation envelopes (§3, C2). | Write `strict_sent` and `wire_conformant` as sibling keys next to `function` in each redacted assistant `tool_calls` entry that P4 writes (`turn_audit.py:230-240`). Carry them there on `_ToolOutcome`. They also go on `ComposerToolInvocation` for the routes that do persist envelopes. **Ruled (lead, provisional; John may overrule): accepted** (§7.3 item 3). | The facts describe the call, not the response. This keeps them out of the response redaction walker and `test_adequacy_guard.py`. Assistant `tool_calls` are never replayed to a provider: `_composer_chat_history` copies only `role`/`content` (`sessions/routes/_helpers.py:1641-1677`, R). |
| D2 | How does the resolver know the routing provider? | Call `litellm.get_llm_provider(model=…, api_base=…)` as a Tier-3 parse into a closed set. `BadRequestError` gives `NONE`. The expected-values table in `test_strict_transport.py` and a fidelity-matrix row pin the resolver's result against LiteLLM's. | This mirrors LiteLLM exactly, including its 47 known OpenAI-compatible hosts (M). A hand-written prefix table would call bare `gpt-5.5` + `api.deepseek.com` "openai" when LiteLLM calls it `deepseek`. |
| D3 | LiteLLM also reads base URLs and versions from env. | The resolver takes an injected `env: Mapping[str, str]` (`os.environ` in production) and reads `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENROUTER_API_BASE` and `AZURE_API_VERSION`. An OpenAI base set in env counts as a custom endpoint. A test pins that ELSPETH never assigns `litellm.api_base` or `litellm.api_version`. | Fails closed. Refusing to boot when those variables are set would be a separate behaviour change. |
| D4 | One setting, or one per role? | One setting covers both the planner route and the hatch route. | R4 names one setting. A per-role override can be added later without changing the wire. Ruling 8 (a non-fatal `hatch_terminal` rejection, D9) removes the case where a hatch-only 400 forces `off` on the planner route too. |
| D5 | How precise should the Azure rule be? | Conservative. The chat api-version (env `AZURE_API_VERSION`, otherwise LiteLLM's default, pinned) supports strict when it is `preview`, `latest`, `v1` or a date of at least `2024-08-01`. LiteLLM's `is_model_gpt_5_4_plus_model` bridge prediction is not copied. | That heuristic keys on arbitrary deployment names. The known false negative (a gpt-5.4+ deployment with an old env version) resolves to `NONE`, which is today's bytes. |
| D6 | OpenRouter detection | Use the advisor's rule: the `openrouter/` prefix, or an api_base host of `openrouter.ai` (`advisor_request.py:41`). Reasoning, branding and usage accounting keep their prefix-only detectors. | Unifying the detectors changes request kwargs outside S1. It is recorded as a follow-on. **Ruled (lead, provisional; John may overrule): follow-on** (§7.3 item 5). |
| D7 | `openrouter/auto` and other meta-models | Resolve to `FORWARDING`. | Honest: the key is forwarded, and whether it is enforced is measured per `provider_served` by `wire_conformant`. |
| D8 | The Anthropic family | Any model for which `supports_anthropic_prompt_cache_markers(model)` is true (`llm_response_parsing.py:773`) resolves to `NONE` on every setting, before any other rule applies. | Native `anthropic/` drops `strict` (M). OpenRouter strips it for Anthropic models unless a beta header is sent. So cache markers and `strict` never share a route. That settles reader surprise 5 in `understand-tools.md`. |
| D9 | How is the hatch-terminal probe budgeted? | New surface `hatch_terminal` with role `planner`, so it gets the 5 s per-request cap. It runs after `planner_tools` and before `advisor`, and only when the hatch resolves non-`NONE`. **Ruled (lead, provisional; John may overrule), §7.3 items 1 and 8:** the request is `build_planner_request_kwargs` at candidate effort with `max_tokens` overridden to 16 (`LOOP_PROBE_MAX_TOKENS`, as `loop_tools` does at `boot_probe.py:157`), so it is a rejection check only; and a `hatch_terminal` 400 is **non-fatal** (logged as `rejected`, boot continues), while a 400 on `loop_tools`, `planner_tools` or `advisor` stays fatal as in S0. | A rejected request returns before generation, so the cap keeps the 400 signal (S0b). Cost: on a 4-surface boot the advisor's share falls from at least 35 s to at least 30 s. |
| D10 | Tool-list builder signatures | `composer_loop_tool_definitions(dialect)`, `planner_tool_definitions(policy=None, *, dialect, terminal_contract=None)` and `planner_terminal_tool_definition(terminal_contract=None, *, dialect)` each take a **required** dialect. There is no zero-argument form. | A defaulted form would be a second path. The tests that are true only for today's bytes become honest by naming the `NONE` dialect. |
| D11 | `ComposerLLMCall.tool_contract_dialect` and `.strict_tool_count` | Derived inside `build_llm_call_record` from the `tools` it receives. The rule: every tool carries a boolean `strict` means `openai_strict`; no tool carries the key means `none`; a mix raises `AuditIntegrityError`; no tools means `None`/`None`. | This ties the record to the same bytes as `tools_spec_hash`, keeps the single construction site, and needs no change at any of the 9 call sites (a grep shows 10 lines; `guided/chat_solver.py:4915` is a comment). |
| D12 | Where does decode reject? | `decode_wire_arguments` raises `ToolArgumentError(category=WIRE_ENVELOPE)`. That is its only rejection in S1. Everywhere else it classifies. | Same style as the S gate (`require_schema_valid_arguments` raises). The producer census resolves `type(exc).__name__` to a caught exception. |
| D13 | Omission guidance outside W | Leave it unchanged: `service.py:981`, `skills/pipeline_composer.md:260/440/644/812` and `planner_authoring_aids.py:934`. | "Pass null" would be wrong on `none` routes and on MCP, where the omission-only fields reject `null`. The skill file is also hashed. The description overrides inside W are what a grammar-bound model reads. This is a named risk with a measurement (§7.1, risk 1). **Ruled (lead, provisional; John may overrule): unchanged** (§7.3 item 4). |
| D14 | Status disclosure | **Ruled (lead, provisional; John may overrule), §7.3 item 2:** the unauthenticated `/api/system/status` publishes only closed, **route-independent** values: `composer_tool_contract = {"setting", "strict_capable_tool_count", "tool_count"}` (32 and 42, properties of the tool set computed from `_WIRE_TOOL_DEFS[OPENAI_STRICT]`, not of any route). Per-route transport and dialect, the effective (sent) strict counts, the resolver diagnostic (D24) and the per-surface boot outcomes go to operator-side structured logs only (T8, T10). A test pins identical public payloads for otherwise-identical normal-OpenRouter-host and custom-endpoint configs. | Transport and effective counts depend on the api_base, the env and the setting, and `system_status` publishes no endpoint base URL (`app.py:2269-2324`, R). Publishing a per-route transport, or the effective count (Codex finding 2: `preferred` + normal OpenRouter host gives 32, the same model + a custom endpoint gives 0), would tell an unauthenticated caller that a custom endpoint is configured; per-surface boot outcomes would disclose provider reachability. A tool-set property discloses neither. |
| D15 | Frontend | Leave the TypeScript `SystemStatus` unchanged. | Nothing in the frontend reads the new key, and the client casts the body (`client.ts:606-609`). |
| D16 | What does `strict_sent` mean? | It mirrors the `strict` key that was sent for that tool: `true`, `false` (an explicit `strict:false` was sent), or `None` (no key was sent: the `none` dialect, or a tool name that was not in the sent list). | With a two-valued flag, "no key" and "explicit false" both read `false`, and the P4 row does not carry the dialect, so Appendix A would mix them. |
| D17 | Decode for a tool name that is not in the sent list | Callers decode only names in the tool list sent on that call. Any other name passes through with its arguments unchanged, `strict_sent=None` and `wire_conformant=None`. `decode_wire_arguments` itself raises `WireProjectionError` for a name it has no W for (a caller bug). The compose loop sends all 42, so its sent set is the whole W map; the planner sends a policy palette, so `_parse_response_tool_calls` receives the sent names. | The planner palette is not enforced at dispatch: `execute_discovery_tool_with_context` admits every discovery handler (`tools/_dispatch.py:916-944`, R), and no palette check sits between `_parse_response_tool_calls` (`pipeline_planner.py:1682-1745`) and `execute_one_discovery` (R). Without the sent set, decode would strip `get_pipeline_state.component: null` on a call whose W was never sent (the palettes never include that tool). |
| D18 | `encode_semantic_arguments` scope | It ships with the `EnvelopeUnwrap` inverse only. The `openai_strict` "write `null` for an absent promoted key" branch is deferred to S2. | C5 limits encode to semantic set_pipeline arguments, and set_pipeline is non-strict on both dialects in S1, so the null branch would have no production reader. Arguments are added with their first reader (the S0b precedent). |
| D19 | T9's reach | The S-gate violations are carried on `ToolArgumentError` on every path (they are built in the shared `_dispatch.py`), but they are rendered as `validation_errors` only by `arg_error_payload`, which is the compose loop (`tool_batch.py:2419`, `:2474`; `service.py:8005`, `:8098`). MCP text and the planner's `_ArgumentErrorResponse` stay unchanged. | MCP builds its own text (`composer_mcp/server.py:781-801`), and planner discovery deliberately projects an argument error without its message or input (`provider_discovery_response.py:186-202`). Extending either is a planner-visible or client-visible contract change outside S1. **Ruled (lead, provisional; John may overrule): the repair signal stays compose-loop only** (§7.3 item 9). |
| D20 | One resolution path for the service and the probe | `strict_transport.resolve_composer_tool_contract(settings, *, env)` resolves both routes and returns an owned frozen summary. `ComposerServiceImpl.__init__` and `build_composer_probe_requests(settings, *, env=os.environ)` both call it. The helper's `env` is required; the probe's `env` defaults to `os.environ` itself (the object the service passes), never to an empty mapping. | An empty default would let the probe resolve without `OPENAI_BASE_URL` while the service resolves with it, and send `strict` to a gateway at boot. A required keyword on the probe would force an edit to `test_composer_against_gateway.py:538`, whose "only appended lines" diff is an S1 invariant (§5). The probe does not read the service object, because it runs on `composer_boot_probe_enabled` alone. A T8 test pins that probe and service resolve the same contract. |
| D21 | `begin_dispatch*` wire-fact keywords | They keep a `None` default (25 test call sites in 11 files; `pipeline_commit.py:514` and guided honestly do not know). Every `tool_batch` site (`:891`, `:948`, `:1010`, `:1061`) and the planner site (`:5033`) passes the facts explicitly, and a pin checks that each invocation's facts equal its `_ToolOutcome`'s. | This closes the gap the default could hide, without 25 mechanical test edits. |
| D22 | Ledger text on a node that has no description | Only one shape exists on the 32: `maxLength` on the `items` of an array property (`request_advisor_hint.recent_errors.items`, `.attempted_actions.items`; the items carry only `type` and `maxLength`, M). The sentence goes on the array property's description as "Each item has at most {n} characters." Any other description-less owner raises `WireProjectionError`. The root `examples` goes into the tool description (C9). | It keeps the text next to the array the model fills, and adds no new `description` nodes. |
| D23 | `strict_profile`'s kind vocabulary and `title` | The closed kind set is the 12 in T1 plus `typeless_subschema`, `array_without_items` and `nested_union`, with the mapping from the design-lane checker published in T1. Allowed keywords are exactly `WIRE_KEYWORD_ALLOWLIST`, so `title` gives `keyword_not_allowlisted`. | S1 imports the S0 allowlist unchanged. The design-lane checker allowed `title` on purpose (`check_strict.py:49-60`), so its advisor negative control is ported without its 6 `title` keys, and the unmodified schema becomes a positive. |
| D24 | A base URL the resolver cannot parse (Codex finding 1) | The resolver parses the selected base at the Tier-3 boundary. Only env-sourced bases can be malformed (settings bases pass `_validate_composer_endpoint_base_url`, `config.py:145-175`, R). If `urlsplit(base)` raises `ValueError`, or its `hostname` is `None`, the route resolves to `NONE` on every setting and the result carries a closed diagnostic naming the variable (`StrictTransportDiagnostic`: `unparseable_openrouter_api_base`, `unparseable_openai_base_url`, `unparseable_openai_api_base`). The value is never logged or published (an env URL can carry userinfo). The resolver never raises for env input. `resolve_strict_transport` therefore returns a frozen `StrictTransportResolution(transport, diagnostic)`, not a bare `StrictTransport`. | M (`$L/plan-review/env_url_probe.log`): `urlsplit('http://[')` raises `ValueError: Invalid IPv6 URL`, `'not-a-url'` gives `hostname None`, and LiteLLM's `get_llm_provider` tolerates all three malformed env variables (so the plan's own parse was the only crash site). T8 calls the resolver in `ComposerServiceImpl.__init__` regardless of probe enablement, so without this a bad env value would stop app construction. Fails closed to today's bytes. |

### 1.4 What S1 does not do

- It does not change S: the flat registry, `get_tool_definitions()`, the pydantic argument models, MCP's list,
  `test_tool_declarations.py` or the `schema_contract` walkers.
- No options carrier, no pair maps and no strict terminal (S2 and S3).
- No `required` setting value, no canary, no endpoint pinning and no breach monitor (S4).
- No Anthropic dialect (S5).
- It does not change ELSPETH's gateway (`gateway/`). Adding `strict` to `ChatFunctionDef` is a separate
  gateway-contract decision.
- No `tool_choice` and no `parallel_tool_calls`, anywhere. No `provider.require_parameters` on the planner.
- No new provider call on any compose transition. The only new provider call is one boot-time probe request.
- It does not unify the OpenRouter detectors (D6), and it does not edit omission prose outside W (D13).
- No DDL and no session-epoch bump. Every new field is additive JSON inside columns that already exist.

### 1.5 S0 facts relied on (all in `85ebf2739`)

- Every one of the 42 loop tools is admitted by the closed-root Draft 2020-12 S gate: `_TOOL_SCHEMA_BY_NAME`
  (`tools/_dispatch.py:444-446`), `require_schema_valid_arguments` (`:563-571`), and the carve-out gates at
  `service.py:7876` and `:8049`.
- `WIRE_KEYWORD_ALLOWLIST` (`tools/_dispatch.py:453-474`, 18 keywords) is the single allowlist constant, and S1
  imports it.
- `ToolArgumentErrorCategory` includes `WIRE_ENVELOPE` (`contracts/composer_audit.py`). The envelope block builds it
  through `_pre_dispatch_argument_error` (`tool_batch.py:999`).
- `ComposerToolInvocation.error_category` and its `__post_init__` cross-check (`contracts/composer_audit.py:230-238`).
- `ComposerLLMCall.provider_served` is in `_LLM_CALL_PUBLIC_AUDIT_FIELDS` (`composer/audit.py:342-373`). Its test,
  `test_llm_provider_served_audit.py:246-274`, is the pattern that the new fields' tests follow.
- The boot probe: `boot_probe.build_composer_probe_requests(settings)` (`boot_probe.py:135`) emits `loop_tools`,
  `planner_tools` and `advisor`. One 45 s shared deadline covers them (`app.py:209`), with a 5 s cap for
  `role == "planner"` (`app.py:773`). `ComposerBootConfigError` names the strict counts (`boot_probe.py:223-239`).
- The request builders `service.build_composer_loop_request_kwargs` (`service.py:906`) and
  `pipeline_planner.build_planner_request_kwargs` (`pipeline_planner.py:1508`) are shared by production and the
  probe.
- The tool-list builder is `service.composer_loop_tool_definitions()` (`service.py:864-903`). It takes no
  arguments today, and S1 adds the argument with its first reader.

### 1.6 The ordering rule (hard)

**No commit may send strict W while STRIP_NULL decode is not running on that path.** Under grammar, the model must
send `null` at the 11 omission-only positions, and S rejects `null` there. The task order below therefore builds
the pure modules first, then wires decode in with every route still on the `none` dialect, which is provably today's
behaviour. Only after that does one commit flip the resolution (T8). Nothing before T8 changes bytes on any route.

---

## 2. Measured starting point

Environment for every command:

```bash
W=$(git rev-parse --show-toplevel); L=$W/.claude/lanes/s1
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$W/src:$W/elspeth-lints/src
PY=$W/.venv/bin/python
cd $W && $PY -c "import elspeth,elspeth_lints;print(elspeth.__file__, elspeth_lints.__file__)"   # both must be under $W
env | grep ELSPETH_JUDGE                                                                            # must print nothing
```

| Fact | Value | Reproduce (log in `$L`) |
|---|---|---|
| Loop tools / strict candidates / option-bearing | 42 / 32 / 10 (names in `understand-tools.md` §2) | `$PY $L/measure_tools.py` → `measure_tools.log` (controls: planted `enum:[…,null]`, planted `not`, discovery∩option `set_source`) |
| Already strict-clean as sent | 21 of the 32 | `$PY $L/check_strict.py --controls` → `check_strict.log` (2 negatives → 0 rows, 11 positives, 1 mutation red) |
| Promoted positions | 13; 11 omission-only (`create_blob.description`, `wire_blob_inline_ref.encoding`, `clear_source.source_name`, `get_pipeline_state.component`, `list_models.provider`, `list_models.limit`, `set_metadata.patch.name`, `set_metadata.patch.description`, `request_advisor_hint.schema_excerpt`, `request_interpretation_review.llm_draft`, `wire_secret_ref.target_id`); 2 already nullable (`get_plugin_assistance.issue_code`, `upsert_edge.label`) | `measure_tools.log` |
| Only enum among promoted positions | `wire_blob_inline_ref.encoding` `[latin-1, utf-16, utf-8, utf-8-sig]`, `default: utf-8` | `measure_tools.log` |
| Enums that already contain `null` in the flat S | 2, both on `upsert_node` (an option tool): `output_mode` `["passthrough","transform",null]` and `scope_policy` `["require_all","best_effort",null]` (`tools/transforms.py:118`, `:124`). They stay on both dialects, because the 10 option tools keep their `none` parameters (C20) | `measure_tools.log` line "enum containing null"; re-measured in review (control: a planted `enum:["x",null]` is found) |
| Strict-clean count under T1's rules | Still 21 of the 32 when allowed keywords are exactly `WIRE_KEYWORD_ALLOWLIST` (symmetric difference with the design-lane checker's 21: empty) | review-round ad-hoc walk (M); T1's inventory test is the pin |
| Ledger entries on the 32 | 15: `maxLength` ×7, `minLength` ×4, `default` ×3, root `examples` ×1. Two `maxLength` owners have no description (`request_advisor_hint.recent_errors.items`, `.attempted_actions.items`), handled by D22 | review-round walk (M) |
| Keywords on the 32 kept by the allowlist | `format: uuid` ×1 (`wire_blob_inline_ref.blob_id`), `minimum` ×1 (`list_models.limit`), `maxItems` ×2 (`request_advisor_hint.recent_errors`, `.attempted_actions`) | `measure_tools.log` |
| Keywords on the 32 dropped to the ledger | `maxLength` ×7, `minLength` ×4, `default` ×3 (all 3 on promoted positions: `encoding`, `source_name`, `limit`), `examples` ×1 (the **root** of `upsert_edge`) | `measure_tools.log` |
| `pattern` on the 32 | 0 (so LiteLLM's regex sanitiser does not touch S1's strict tools) | `$PY $L/pattern_census.py` → `pattern_census.log` |
| Descriptions on the 32 that say "omit" | **6**: `get_pipeline_state /properties/component`, `get_plugin_assistance` tool description, `get_plugin_assistance /properties/issue_code`, `list_models /properties/provider`, `request_interpretation_review /properties/kind` ("OMIT llm_draft"), `request_interpretation_review /properties/llm_draft` | `$PY $L/omit_census.py` → `omit_census.log` (control: planted "Omit x" found) |
| Planner lists | `planner_tool_definitions()` = 19 discovery + terminal. The 19 are a subset of the 32, disjoint from the 10, and byte-equal to their loop entries. The production palette is 15 tools on freeform, 14 on the tutorial, at most 18, and never includes `get_pipeline_state` (`PlannerDiscoveryPolicy.initial`, `pipeline_planner.py:323-364`) | `measure_tools.log`, `planner_intersect.log` |
| **Production planner palettes (NONE-route invariant, part 2)** | Production always passes a policy (`pipeline_planner.py:3775`, `:5217`), so the `policy=None` list below is not what is sent. `PlannerDiscoveryPolicy.initial(FREEFORM)` (= `GUIDED_FULL`): 16 tools, **25,424 B, `709b417659b912b656f9e07f4d67e492cca6265caf9d07238969f55bb256eca4`**. `initial(TUTORIAL_PROFILE)` (= `GUIDED_STAGED`): 15 tools, **23,960 B, `50fdbefd807bc2ba63dde69468ffc61201170b2c030271c7a5ac76a40d8e5479`**. Same compact UTF-8 form | `palette_bytes_base.log` (M, review round). `tool_bytes.py` is extended in T2 to print these too |
| **Tool bytes (the NONE-route invariant)** | loop: 42 tools, **63,872 B, SHA-256 `6c7f60dde72b3858b93f3fa2eaebc5700663d341d8f1e362fdb8b8bfd962e499`**; planner: 20 tools, **29,264 B, SHA-256 `7e9582451ed5610b27c1eb3c008300d5ea0e8177933d93cfde33b3658cad6f5a`**. This hash is blind to tuple versus list (a tuple thaw gives the same bytes and hash, M), so it is branch evidence only; the type-sensitive `==` pins in T2 guard the freeze/thaw round trip. Instrument form: `json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`. The default ASCII-escaped form gives 64,055 / 29,378, so always state the form. The S0 note's "96,240 bytes for both lists" is 24 B off a default-separator sum (96,216); it is unreconciled and does not affect this pin | `$PY $L/tool_bytes.py` → `tool_bytes_base.log` (control: a 1-character mutation changes the hash). After T6/T7 the script must pass `ToolContractDialect.NONE` to the builders |
| Compose loop persists no invocation envelopes | 3 assistant rows, 3 tool rows with `tool_calls = NULL`, 0 `_kind=audit` envelopes over the SUCCESS→ARG_ERROR→PLUGIN_CRASH canary | `$L/test_p4_envelope_probe.py` → `p4_envelope_probe.log` |
| LiteLLM | 1.102.0. The provider resolution facts are in `provider_matrix.log`; strict forwarding per route is in `wire_recorder.summary.txt`; hosted-openai and bridge rows (recorded with respx) are in `respx_hosted_probe.log`; the gateway rejects both `strict:true` and `strict:false` (`gateway_contract.log`) | see `understand-transport.md` |
| Gate baselines at `85ebf2739` | 7 pinned files 413 passed; whole-tree set 510 passed; `check_contracts` exit 0 (soft census 2756 / 398 / 65); masquerade `--check` 119; skill inventory `--check` exit 0; trust-tier corpus exit 1, **2290** lines (`lints-base.findings`); compose-loop byte envelope 216,295 B against the 300,000 ceiling | `understand-gates.md` §0 |
| Trust-tier corpus comparison instrument | `lints-base.findings` is **not sorted** (`LC_ALL=C sort -c` fails at line 2, M), and each line carries `path:line:col:`, so a raw `comm` reports about 4,540 false differences, and even with both sides sorted a pure line shift (no new finding) reports 76 (test critic's measurement). The normalised form (`lints-base.norm`, 2290 lines) strips `:line:col:` and sorts under `LC_ALL=C`; compared with `diff`, it is a multiset comparison. Controls (M): base against itself → 0 lines; the critic's line-shifted copy → 0 lines; one planted finding → exactly 1 line | G-tier in §4 |
| Test hermeticity | pytest loads `.env` (`pyproject.toml:486`), and no test or conftest scrubs `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENROUTER_API_BASE` or `AZURE_API_VERSION` (grep over `tests` and `.github/workflows`: 0 files; positive control `OPENAI_API_KEY`: found). The main checkout's `.env` holds none of the four today, so the exposure is latent | T8 adds the scrub |

---

## 3. Corrections to the master plan

These are the places where the live code disagrees with the master plan's S1 section (or the sections it relies
on). The tasks below follow the correction. Record each one in the master plan's "S1 as implemented" paragraph
(T12).

- **C1. A bare model plus an api_base is not always provider `openai`** (§2.2 row 3; S1 strict_transport bullet).
  LiteLLM checks api_base against 47 known hosts first (`get_llm_provider_logic.py:241-374`). Measured
  (`provider_matrix.log`): `gpt-5.5` + `https://api.deepseek.com/v1` → `deepseek`, and a bare `claude-*` with any base
  → `anthropic`. The resolver asks LiteLLM (D2), so `forward_to_endpoint` never counts those routes as forwarding.
- **C2. The compose loop does not persist `ComposerToolInvocation`** (S1 `tool_batch.py` bullet; §5.3; Appendix A).
  Measured (`p4_envelope_probe.log`): P4 writes redacted assistant `tool_calls` and tool-row content, and the route
  drain is skipped once P4 has persisted (`service.py:5473` and six more sites; `routes/messages.py:997`). Fields
  placed only on the invocation would never reach storage on the route S1 measures. The fix is D1, and Appendix A
  reads the facts from the `calls` CTE.
- **C3. Planner decode placement** ("after `_parse_json_object` (`pipeline_planner.py:1581`)"). `_parse_json_object`
  is at `:1627`, called at `:1737`. `_ParsedToolCall.arguments` is frozen at `:1738` and read **before dispatch** by
  `_tool_information_keys` (`:441`), the `get_pipeline_state` component read (`:3241`), the cycle guard's
  `stable_hash(call.arguments)` (`:4984`) and `mark_schema_loaded` (`:5181`). Decode therefore runs inside
  `_parse_response_tool_calls` (`:1682`), before `_ParsedToolCall` is built, for non-terminal calls only.
- **C4. Planner stamping has a hard ordering constraint the master plan does not state.** `call_model` marks, then
  snapshots and hashes `active_tools` into the capability manifest (`pipeline_planner.py:3869-3894`), and
  `_assert_planner_call_matches_manifest` (`:1577-1590`) requires `tools_spec_hash == manifest.effective_tool_hash`.
  Stamping must happen before `:3869`, and the api_base choice now made inside the attempt loop (`:3972-3978`) must
  move above it (a pure move: same condition, same values).
- **C5. `encode_semantic_arguments` is not "used by `_replace_llm_tool_call_arguments`" in general.** 3 of that
  function's 7 callers pass redaction sentinels (`tool_batch.py:834`, `:886`, `:1005`), and set_pipeline sentinels are
  re-enveloped today (M, `dispatch_probe.log`). The 32 strict tools only reach it with sentinels. Encode is applied
  only to semantic set_pipeline arguments, and sentinels keep today's bytes.
- **C6. Six omission instructions, not five, and not only at promoted positions** (§3.3 rule 3 lists 4 properties
  plus 1 tool description). `request_interpretation_review /properties/kind` also says "OMIT llm_draft"
  (`omit_census.log`). The faithfulness gate scans **every** description in a strict-capable W, not only the
  promoted positions.
- **C7. The prototype puts `null` inside an `enum`** (the §2.1 byte delta 28,464 → 28,814 was measured on that form).
  `prototype_projection.make_nullable` appends `None` to `wire_blob_inline_ref.encoding`'s enum, which §3.3 rule 3
  forbids, and `check_strict` has no rule for it. `strict_profile` gains that rule, the projection uses `anyOf`, and
  the byte delta is re-measured on the form that ships (T2).
- **C8. Promoted positions with a `default` need null-specific ledger text.** The 3 defaults (`encoding`,
  `source_name`, `limit`) render as "Pass null to use the default (X)." Plain "default X" would bring back an
  omission cue.
- **C9. `examples` sits at a tool root** (`upsert_edge`), so its ledger text goes into the tool description.
- **C10. The S-gate repair signal must not echo the offending key** (S1 "S-gate repair signal": "and the offending key
  for additionalProperties"). The key is authored by the model. The existing pydantic canonicaliser emits only
  closed schema-owned names or `field`/`item` (`audit.py:1254-1360`). The S-gate carrier follows the same rule:
  parent path plus type `unexpected`. Separately, `arg_error_payload` (`tool_error_payloads.py:33-38`) emits
  `validation_errors` only from a pydantic `__cause__`, so a new carrier is needed.
- **C11. Azure** (S1 bullet "`azure/` is ENFORCING only when its api-version supports strict … `2024-08-01-preview`").
  The chat default is already `2025-02-01-preview` (`litellm.AZURE_DEFAULT_API_VERSION`, M). On the Responses
  bridge, the version is `preview` whatever `AZURE_API_VERSION` says (M), so `preview`, `latest` and `v1` must count
  as supporting strict. `azure/` always has a base, so the "custom api_base → NONE" rule applies only to the
  `openai` and `openrouter` routing providers. `azure_ai/` has no row in §2.2 and resolves to `NONE`. Under ruling 7
  (§7.3), a supporting api-version makes Azure `ENFORCING` only under `forward_to_endpoint`; under `preferred` it is
  `NONE` until R8.
- **C12. `openrouter/` with a non-OpenRouter base** (a proxy, or `OPENROUTER_API_BASE`) is not covered by the
  custom-endpoint rule, which names only `openai/` and bare names. It resolves to `NONE` unless the setting is
  `forward_to_endpoint`.
- **C13. The gateway rejects explicit `false` too** (M, `gateway_contract.log`). Under `forward_to_endpoint`, the 10
  explicit-false tools alone would fail boot against ELSPETH's gateway.
- **C14. The hatch probe does not fit the S0 budget shape as written.** Resolved by D9. It needs a surface name, a
  role, a cap and a place in the order.
- **C15. `/api/system/status` cannot publish per-route facts** (superseded by ruling 2, §7.3; the first draft said it
  "needs one entry per route" and that T10 would store `probe_status`). The two routes can resolve differently (an
  OpenRouter planner with an `anthropic/` hatch: `FORWARDING`/`NONE`), and any per-route transport or effective strict
  count reveals whether a custom endpoint is configured (D14). The public key carries only the setting and
  tool-set properties; per-route facts and the per-surface `probe_status` (a local of the lifespan loop,
  `app.py:752-838`, R) go to structured logs.
- **C16. Gates the S1 list omits:** `composer.exception_channel` (CEC1, bare `ValueError`/`TypeError` in
  `web/composer/tools/*` other than `_dispatch.py`, M); `check_contracts` Check 2 (`dict[str, Any]`
  params/returns need `contracts-whitelist.yaml` rows); the composer wire census's AST binding of
  `arguments` in `run_tool_batch` (`scripts/cicd/composer_wire_census.py:406-437`); the new `R_TB_SUPPRESSED`
  corpus lines from the decode boundary; `test_composer_llm_call_construction_sites.py`;
  `test_error_class_producer_census.py`. The census "is unaffected because it reads S" is true for W, but not for a
  `tool_batch` rename.
- **C17. Which tests change wire at the flip** (rewritten for ruling 7; the first draft said the default test model
  changes the wire in almost every compose-loop unit test). Under ruling 7, bare `gpt-5.5` with no base resolves to
  `NONE` under `preferred` (row 7), and `WebSettings`' defaults are `gpt-5.5` and `anthropic/claude-sonnet-4-6`
  (`config.py:256`, `:361`, R), so tests on the defaults keep today's bytes. What changes: any test that names an
  OpenRouter planner with no custom base (row 4), and any test whose advisor resolves non-`NONE`. `test_boot_probe.py`'s
  fixture default advisor is `openrouter/z-ai/glm-5.3` (`:41`, R), which resolves to `FORWARDING`
  (`supports_anthropic_prompt_cache_markers` is `False` for it, M `$L/plan-review/d8_probe.log`), so the
  `hatch_terminal` surface appears in every test there that does not override the advisor. Its `gpt-5.5` loop and
  planner pins (`:76`, `:79`, `:93`, `:316`) and the mutation control (`:96-106`) stay as they are (T8). Tests that
  need the strict wire name an OpenRouter planner, or set `forward_to_endpoint`.
- **C18. The fidelity matrix must pin the cost map.** A bare `import litellm` loads the remote model map (M). The
  matrix asserts the source is `local`, and T11 makes that hold on purpose (C24). The design-lane evidence imported LiteLLM
  first, but the relevant keys are equal today (`costmap_compare.log`), so it stands.
- **C19. Stale citations.** Master plan → now: `service.py:7719 _get_litellm_tools` → deleted, the builder is
  `service.py:864-903` (envelope `:885-891`); `tool_batch.py:910/929` non-object → `:938`; envelope block
  `:961-1008` → `:995-1045`; `_replace_llm_tool_call_arguments :433` → `:450-485`; required paths `:1170,1177` →
  `:1194-1232`; `pipeline_planner.py:1504/1481` → `:1550/1485`; hatch `:4225-4226` → `:4255-4262`; cache-marker
  choice `:3820-3827` → `:3869-3873`; `_parse_json_object :1581` → `:1627`/`:1737`; `llm_response_parsing.py:768` →
  `:797`; `service.py:4357/4362, 4769/4774, 5276/5281` → `4440/4445, 4852/4857, 5359/5364`; `/api/system/status
  app.py:2198` → `:2268`; LiteLLM `gpt_transformation.py:448-462` → `:437-462` (the hosted-host test is
  `openai.com`/`*.openai.com`, `:407-419`).
- **C20. "No `enum` anywhere in any W contains `None`" is false on the base** (master plan S1 test list; this plan's
  first draft of T2 check 6). The flat S already has two, on `upsert_node` (§2). They are S, so they stay in the
  `none` W and in the `openai_strict` W of the 10 option tools. The check and its test apply to strict-capable W
  only.
- **C21. The planner palette is not enforced at dispatch** (the first draft of T7 said an unsent discovery name
  "is rejected by the S gate"). `execute_discovery_tool_with_context` admits every discovery handler
  (`tools/_dispatch.py:916-944`, R). Such a call is dispatched today and S1 keeps that; S1 only stops decode from
  touching it (D17).
- **C22. The repair signal's only renderer is the compose loop** (master plan "S-gate repair signal"). See D19.
- **C23. `strict_sent` is three-valued** (master plan: `bool | None`, `None` "where no arguments were decoded"). See
  D16. `wire_conformant` keeps the master plan's meaning: `None` exactly where no arguments were decoded.
- **C24. The fidelity matrix's `local` cost-map precondition holds today by accident.** `configure_litellm_pricing()`
  is called only at `core/llm_pricing.py:29` and `plugins/llm/model_catalog.py:71`, not in `elspeth/__init__.py`
  (R). Under pytest the env var becomes `True` only because `tests/unit/web/conftest.py` imports ELSPETH modules
  that load it before LiteLLM (measured by the test critic). "Import ELSPETH first" is therefore not a rule an
  implementer can follow; T11 calls the function in the root `tests/conftest.py`.

---

## 4. Task list

The tasks run in dependency order, one commit per task unless the task says otherwise.

Rules for every task:

- **RED first.** Write the tests, run them, and record the failure and its **actual** reason before changing
  production code. If the reason is not the one the task names (for example a `TypeError` at construction rather
  than the behaviour under test), record the actual one.
- **Characterization tests are named as such.** A test that is green on arrival (it pins behaviour that already
  exists, such as T11's matrix rows and gateway rows) is labelled "characterization" in its docstring and in the
  task. It still needs its mutation control, which is what shows it can go red.
- **Mutation control.** Each new test gets a named mutation that turns it red. Run the mutation once and record
  the log in `$L`, then revert it. Flipping an expected value only proves the assert runs; a control must mutate
  the thing under test (production code, the input, or the instrument's input).
- **Focused runs:**
  `cd $W && $PY -m pytest <files> -n 0 -p no:cacheprovider > $L/<task>-<name>.log 2>&1; echo "exit=$?"`.
  Read the exit code, not a tail.
- **Commit:** run `git status --short`, then `scripts/branch-safety-check.sh --intent commit`, then
  `git commit -- <pathspecs>` (never `git add` then a bare commit). End the message with the Co-Authored-By line
  from the session's attribution reminder.
- **Whole-tree gate list.** Run the gates below that a task names, and each one whose scanned inputs the task
  changes. G-mock, G-walker, G-masq, G-attr, G-tier and G-ruff/mypy scan all of `src` and `tests`, so **every task
  that commits code runs them** (the **every-task set**), whether or not its own gate line repeats them. The
  per-task lists below name the extra gates.
  - **G-mock:** `$PY -m pytest $W/tests/unit/test_mock_discipline_baseline.py -n 0 -p no:cacheprovider` (every
    `Mock`/`MagicMock`/`AsyncMock` needs a `spec`).
  - **G-walker:** `$PY -m pytest $W/tests/unit/elspeth_lints/test_python_file_walker_authority.py -n 0 -p no:cacheprovider`
    (no literal `rglob("*.py")`/`os.walk(` under `tests/`, **comments and docstrings included**; use
    `tests/helpers/tree_gate.iter_gate_files`/`iter_gate_sources`).
  - **G-masq:** `$PY -m elspeth_lints.rules.masquerade.seed_baseline --check` (must stay at 119, no reseed) and
    `tests/unit/elspeth_lints/test_masquerade_gate.py`. No `getattr`/`hasattr` in new code or tests.
  - **G-attr:** `tests/unit/web/test_sessions_composer_attribute_contracts.py` and `tests/unit/test_no_hasattr_branching.py`.
  - **G-contracts:** `$PY -m scripts.check_contracts`. Re-pin the soft census with `--write-census` in the same commit;
    the diff must show only the files this task touched. Add a Check 2 row in `config/cicd/contracts-whitelist.yaml`
    (with a reason comment) for every new `dict[str, Any]` parameter or return. Do not swap to `Mapping[str, Any]`
    to dodge Check 2, because the census counts that form. Then run
    `$PY -m pytest $W/tests/unit/scripts/test_check_contracts.py -n 0 -p no:cacheprovider`.
  - **G-wire:** `$PY -m pytest $W/tests/unit/scripts/test_composer_wire_census.py $W/tests/unit/scripts/test_composer_wire_scorecard.py $W/tests/unit/web/composer/test_tool_model_wire_parity.py $W/tests/unit/web/composer/test_tool_knob_teaching_gate.py -n 0 -p no:cacheprovider`.
  - **G-census-S0:** `tests/unit/web/composer/test_error_class_producer_census.py`, `test_tool_argument_error_category.py`,
    `test_error_code_redaction.py`, `tests/unit/architecture/test_composer_llm_call_construction_sites.py`.
  - **G-skill:** `$PY $W/scripts/cicd/generate_skill_inventory.py --check` (must stay exit 0; S1 adds or renames no tool).
  - **G-decl:** `$PY -m pytest $W/tests/unit/web/composer/test_tool_declarations.py -n 0 -p no:cacheprovider`. It must
    pass **with no edit to the file** (`git diff --stat -- tests/unit/web/composer/test_tool_declarations.py` empty).
  - **G-tier:** `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing $PY -m elspeth_lints.core.cli check --rules all --root src/elspeth > $L/<task>-lints.log 2>&1; echo exit=$?`,
    then normalise and compare as a multiset against the normalised base (§2 "Trust-tier corpus comparison
    instrument"):
    ```bash
    norm() { sed -E 's/^([^:]+):[0-9]+:[0-9]+:/\1:/' "$1" | LC_ALL=C sort; }
    grep -v '^WARNING' $L/<task>-lints.log > $L/<task>-lints.findings
    norm $L/<task>-lints.findings > $L/<task>-lints.norm
    diff $L/lints-base.norm $L/<task>-lints.norm > $L/<task>-lints.diff; echo diff-exit=$?
    ```
    Never run `comm` on the raw files: the base is unsorted and line numbers shift with every edit. Compare the
    multiset difference with the base, not the count with zero. Record every added (`>`) line as expected (with its
    reason) or fix it, and account for every removed (`<`) line. Stage no signatures. After any `@trust_boundary`
    change, also run `--rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier`.
  - **G-cec:** CEC1 is covered by G-tier's `--rules all`. For a quick probe, run
    `--rules composer.exception_channel --root src/elspeth`.
  - **G-ruff/mypy:** `$W/.venv/bin/ruff check <files>` and `ruff format --check <files>`;
    `$PY -m mypy <changed src files>`.
- **Merge-order note.** Two `fix/5887-*` branches append 42 lines to the end of
  `tests/unit/web/composer/test_pipeline_planner.py` (hunk at `@@ -4773`) and add one line at `pipeline_planner.py:2196`.
  Put every **new** S1 test in a new file. S1's edits inside `test_pipeline_planner.py` are the `_model()` helper
  (`:629`) and the two planner-list calls (`:823`, `:2107`), all interior lines that do not overlap the appended
  hunk; the appended tests construct no `PlannerModelConfig` (measured by the risk critic). The other existing test
  files S1 edits mechanically (T2: the 10 loop-list callers; T6: the two `dialect=`/`semantic=` calls; T7: the other
  planner-list callers and the parity monkeypatch; T8: `test_boot_probe.py`, `test_app.py`,
  `test_boot_probe_production_parity.py` and the two conftests) are not in the 5887 file lists (re-checked after the
  Codex round: `grep` over `$L/branch-fix_5887-*.files` finds none of them; positive control
  `test_pipeline_planner` is found).
  Whoever lands second re-runs `check_contracts --write-census` and retakes the corpus baseline.

### T0 — Preflight (no commit)

1. Confirm provenance and the empty `ELSPETH_JUDGE` environment (§2).
2. `git log --oneline -1` must show `85ebf2739`, or the branch tip that S1 is meant to land on. If `release/0.8.1`
   has moved, rebase first, then retake `$L/lints-base.findings`, the census baseline and the `tool_bytes.py` hashes
   (loop, `policy=None` planner and the two production palettes) on the new base, and record the new values in
   this file's §2.
3. Build the normalised corpus base: `norm $L/lints-base.findings > $L/lints-base.norm` (the `norm` function from
   G-tier). Run its three controls and keep the log: the base against itself gives 0 lines, a copy with every
   `service.py` line number shifted gives 0 lines, and a copy with one planted finding gives exactly 1 line.
4. Keep the base export for red attribution later:
   `mkdir -p $L/base-export && git archive 85ebf2739 | tar -x -C $L/base-export` (a directory that is not a
   worktree, so it cannot be mistaken for one).

### T1 — `tools/strict_profile.py`: promote the strict checker

- **Create** `src/elspeth/web/composer/tools/strict_profile.py`.
  - `StrictViolationKind(StrEnum)`, a closed set of 15 (D23): `root_not_object`, `root_union`, `nested_union`,
    `missing_additional_properties_false`, `free_form_object`, `optional_property`, `required_not_in_properties`,
    `ref`, `typeless_subschema`, `array_without_items`, `unsupported_keyword`, `keyword_not_allowlisted`,
    `format_not_supported`, `pattern_not_portable`, `enum_contains_null`.
  - Keyword rules, so that each keyword gives exactly one kind: `oneOf`/`allOf` give `root_union` at the root and
    `nested_union` below it (not also `unsupported_keyword`); `$ref`/`$defs`/`definitions` give `ref`; any other key
    in the design-lane `UNSUPPORTED` set gives `unsupported_keyword`; any other keyword outside
    `WIRE_KEYWORD_ALLOWLIST` gives `keyword_not_allowlisted` (this includes `title`, `default`, `minLength`,
    `maxLength`, `examples`). The design lane's `uncertain_keyword` does not exist here.
  - `StrictViolation` is a frozen dataclass with `tool`, `path` (JSON pointer), `kind` and `detail`.
  - `check_openai_strict(schema, *, tool) -> tuple[StrictViolation, ...]` walks the schema.
    - It uses `type(x) is dict` / `type(x) is list`, never `isinstance`. The measured tier probe gave no finding for
      that form; S definitions are fresh plain dicts at every level.
    - Allowed keywords come from `WIRE_KEYWORD_ALLOWLIST` (import it from `_dispatch`). `additionalProperties` is
      allowed only as `false`, `format` only with the 9 OpenAI values, and `pattern` only when Python `re` compiles
      it and it contains no ECMA-only escapes (`\p`, `\P`, `(?<`).
  - Port the rules from `$L/check_strict.py` (`UNSUPPORTED`, `OPENAI_FORMATS`, `REF_KEYS`, `DATA_KEYS`) and **add
    `enum_contains_null`**: any `enum` array that contains `None` (C7).
  - It raises nothing, and returns rows. The module must contain no bare `ValueError`/`TypeError` (CEC1).
- **Tests (RED: the module does not exist, so the import fails):**
  `tests/unit/web/composer/test_strict_profile.py`. Every check is an **exact** kind-set match (the design-lane
  instrument used a subset check, `expected <= got`).
  - Negatives (0 rows each): the hand-written strict schema, and the `advisor_response_format` schema **with its 6
    `title` keys removed** (at the root and at `verdict`, `category`, `steps`, `findings`, `note`).
  - Positives, with the expected sets rewritten for this vocabulary:

    | Design-lane control | Expected kinds here |
    |---|---|
    | optional + free_form (options) | `free_form_object`, `optional_property` |
    | `additionalProperties: true` | `free_form_object` |
    | `additionalProperties: <schema>` map | `free_form_object` |
    | missing `additionalProperties` | `missing_additional_properties_false` (was `missing_additionalProperties_false`) |
    | root `oneOf` + typeless members | `root_union`, `typeless_subschema`, `optional_property` |
    | nested `oneOf` | `nested_union`, `typeless_subschema` |
    | `$ref` | `ref` |
    | `not` + `pattern "^a"` + `default` + `minLength` + `format: uri` | `unsupported_keyword` (`not`), `keyword_not_allowlisted` (`default`, `minLength`), `format_not_supported` (`uri`); the portable `pattern` gives nothing (was `unsupported_keyword`, `uncertain_keyword`) |
    | array without `items` | `array_without_items` |
    | root not object | `root_not_object` |
    | `required` names an absent property | `required_not_in_properties` |
    | **new:** the unmodified advisor schema | `keyword_not_allowlisted`, exactly at the 6 `title` paths |
    | **new:** `{"type":"object","properties":{"a":{"enum":["x",None]}},"required":["a"],"additionalProperties":false}` | `enum_contains_null` |
    | **new:** a property with `"pattern": "\\p{L}"` | `pattern_not_portable` |

  - Mutation controls: delete the `enum_contains_null` rule and its positive goes red; delete `title` from the
    advisor copy's strip list and the negative goes red. Record the logs.
  - Inventory: over `get_tool_definitions()` as flat S, exactly 21 of the 32 candidate names have zero rows (§2),
    and all 10 option tools have `free_form_object`. The two sets are pinned by name.
- **Gates:** G-cec, G-tier (a new `src` module), G-contracts (new signatures take `Mapping[str, Any]` schemas, so
  re-pin the census), plus the every-task set.
- **Commit:** `feat(composer): strict_profile — OpenAI strict-mode schema checker with controls`.

### T2 — `tools/wire_projection.py` (part 1): projection, ledger, limits, faithfulness gate

- **Create** `src/elspeth/web/composer/tools/wire_projection.py`:
  - `WireProjectionError(Exception)`, owned. Every import-time failure raises it (CEC1).
  - `ToolContractDialect` is imported from `elspeth.contracts.composer_llm_audit`. Define it there in this task:
    `class ToolContractDialect(StrEnum): OPENAI_STRICT = "openai_strict"; NONE = "none"`. It lives in `contracts/`
    because the LLM-call contract reads it, and Check 1 forbids `contracts → web`.
  - Decode-plan nodes (frozen dataclasses): `EnvelopeUnwrap(key="pipeline")` and `StripNull(path: tuple[str, ...])`.
  - `LedgerEntry(path, keyword, value)` and `WireTool(name, dialect, function: Mapping, strict_capable: bool, decode_plan: tuple, ledger: tuple, promoted_paths: frozenset)`.
    - Store `function` deep-frozen, and give a thawing accessor that returns `list` for every array (`enum`,
      `required`, `type` unions) and keeps key order.
    - The `none` identity hash (`6c7f60dd…`) guards that freeze/thaw round trip. A mismatch there is a projection
      bug to fix, not a pin to move.
  - `project_tool(definition, dialect) -> WireTool`, one pure walk:
    - **`none`:** `parameters` is the flat schema unchanged, except that set_pipeline gets the
      `{"type":"object","properties":{"pipeline":<flat>},"required":["pipeline"],"additionalProperties":false}`
      envelope. That is the block moved out of `service.py:885-891`, with the same key order. `decode_plan` is
      `(EnvelopeUnwrap,)` for set_pipeline and empty otherwise. The ledger is empty. `strict_capable=False`.
    - **`openai_strict`:**
      - If `check_openai_strict(flat)` reports `free_form_object`, the tool is not strict-capable. It keeps the
        `none` parameters (set_pipeline keeps the envelope and the `EnvelopeUnwrap` node) and is stamped `false`
        later.
      - Otherwise apply rules 2-5 of the master plan's §3.3:
        - every S-optional property becomes required and nullable: `type: [T, "null"]` for scalars, arrays and
          objects; `anyOf: [{…enum…}, {"type":"null"}]` for enums; an existing `["string","null"]` stays as it is.
          Each promotion adds a `StripNull(path)` node.
        - Keywords outside `WIRE_KEYWORD_ALLOWLIST` move to the ledger.
        - Ledger text is appended to the owning description from a closed template map:
          - `maxLength` → "At most {n} characters."
          - `minLength` → "At least {n} characters."
          - `maxLength` on the `items` of an array property that has no description of its own → "Each item has at
            most {n} characters." on the array property's description (D22)
          - `default` at a promoted position → "Pass null to use the default ({v})." (C8)
          - root `examples` → "Example arguments: {canonical JSON}." in the **tool** description (C9)
          - anything else, including any other owner with no description, raises `WireProjectionError`.
      - Then apply `_STRICT_DESCRIPTION_OVERRIDES` (below). The result must pass `check_openai_strict` with zero
        rows, or the build raises.
  - `_STRICT_DESCRIPTION_OVERRIDES`: a closed map `(tool, pointer) → (exact old substring, replacement)` for the 6
    sites in `omit_census.log`. Each replacement says "pass null" where the text said "omit"; for example
    `list_models /properties/provider`: "Omit to get a provider summary" → "Pass null to get a provider summary".
    For `request_interpretation_review` `/properties/kind` and `/properties/llm_draft`, the replacement keeps the
    meaning: "pass null for llm_draft — the server computes/resolves …", and "if provided it must byte-match the
    staged draft" is kept. A missing old substring raises (a stale override).
  - A pure builder, `build_wire_tool_defs(definitions, *, limits) -> Mapping[ToolContractDialect, Mapping[str, WireTool]]`,
    does the whole projection, the limit check and the faithfulness check for the definitions it is given. Its only
    inputs are its arguments. The fail-closed controls in the tests call it with an edited copy of the definitions or
    a lowered copy of the limits; they never `importlib.reload` the module (a reload mints a new
    `WireProjectionError` class, so `pytest.raises` on the old one fails for the wrong reason).
  - `_WIRE_TOOL_DEFS = build_wire_tool_defs(get_tool_definitions(), limits=OPENAI_STRICT_LIMITS)`, built once at
    import in registry order (`wire_secret_ref` last), frozen.
  - `wire_tool_definitions(dialect) -> list[dict[str, Any]]` returns fresh LiteLLM function dicts in order:
    - `none`: `{"type","function":{name,description,parameters}}`, with **no `strict` key**;
    - `openai_strict`: the same plus `function.strict = strict_capable` (`True` on 32, `False` on 10).
  - `stamp_planner_terminal(definition, dialect)` returns a copy with `function.strict = False` on
    `openai_strict`, and an unchanged copy on `none`.
  - `WireLimitsReport` (total properties, max depth, total enum values, total characters, strict tool count) is
    computed per dialect at import against the OpenAI limits in master plan §2.2 (5000 properties, depth 10,
    120,000 characters, 1000 enum values). Exceeding any of them raises `WireProjectionError`. The limits are a
    module constant, so a test can pass a lowered copy.
  - `assert_wire_projection_faithful(defs, definitions)` runs inside `build_wire_tool_defs`, so it runs at import.
    It checks:
    1. every strict-capable W passes `check_openai_strict` with 0 rows;
    2. every wire-required, flat-optional property is nullable and has a `StripNull` node, and vice versa;
    3. every flat-required property is wire-required and not newly nullable;
    4. every ledgered keyword sits on a tool that is in `_TOOL_SCHEMA_BY_NAME` (so it is S-gated);
    5. no description anywhere in a strict-capable W (tool and property level, case-insensitive) matches the closed
       omission vocabulary `\bomit`, `\bleave (it |this )?(out|blank|empty|unset)`, `\bif not (provided|given|set|supplied)`,
       `\bnot provided\b`, `\b(if|when) absent\b`, `\bdo not (send|pass|include|provide)\b`, apart from one named,
       reviewed exception: the `get_pipeline_state` tool description's "do not send its source/nodes/edges/outputs
       fields at the top level", which is about the set_pipeline envelope, not a promoted position. Every override
       is live (C6). Measured on the flat S of the 32 (review round, M): this vocabulary finds exactly the 6
       override sites plus that one exception (control: "Leave blank to reset" matches, "vomit" does not). The vocabulary is
       a closed list and says so: a phrasing outside it (for example "a full-state read (no component …)") is not
       caught;
    6. no `enum` anywhere in a **strict-capable** W contains `None` (C20: the `none` W and the option tools keep
       `upsert_node`'s two null-bearing enums, because they are S);
    7. on `none`, the **thawed output** (what `wire_tool_definitions(NONE)` returns, not the frozen store) is equal,
       with type-sensitive `==`, to `get_tool_definitions()`'s parameters, apart from the set_pipeline envelope.
  - The module has no `getattr`/`hasattr`, uses `type(x) is dict`, and raises only `WireProjectionError`.
- **Modify** `service.composer_loop_tool_definitions` → `composer_loop_tool_definitions(dialect: ToolContractDialect)`,
  returning `wire_tool_definitions(dialect)`. Move the docstring's envelope paragraph into `wire_projection`.
  **In this commit every production caller passes `ToolContractDialect.NONE`** (`service.py:7161`,
  `boot_probe.py:146`), and each of the 10 test files that call it zero-arg passes `ToolContractDialect.NONE`. That is
  a mechanical pin move (list: `understand-gates.md` surprise 6, or `grep -rln "composer_loop_tool_definitions(" tests`).
- **Tests (RED: the module does not exist):** `tests/unit/web/composer/test_wire_projection.py`. The fail-closed
  controls below call `build_wire_tool_defs` with an edited deep copy of `get_tool_definitions()` or a lowered copy
  of the limits.
  - Partition by name: `strict_capable` on `openai_strict` is exactly the 32 names in §2 and false on the 10.
  - 13 `StripNull` paths, pinned by path. 11 of them are positions where the flat S rejects `null`: check each
    flat subschema by validating `null` with `Draft202012Validator`, not by reading the text. The other 2
    (`issue_code`, `label`) accept `null`.
  - No `enum` contains `None` in any strict-capable W. The `none` W carries exactly the two `upsert_node` enums of
    §2, pinned by path (so a new one is noticed). Control: give one of the 32 an `enum: [..., None]` and build, and
    `WireProjectionError` is raised.
  - The omission vocabulary (check 5) finds nothing in any strict-capable W. Controls: plant "Omit x" on a promoted
    property and it raises; plant "Leave blank to reset" and it raises; delete one override and it raises; remove
    the named `get_pipeline_state` exception and it raises.
  - An unknown keyword fails closed. Control: add `"x-foo": 1` to one flat definition and build, and it raises.
  - The ledger renders into descriptions. For each of the 15 ledger entries on the 32, the rendered sentence is
    present and the keyword is absent from W. The 3 defaults say "Pass null to use the default". The two
    `request_advisor_hint` item limits say "Each item has at most …" on the array descriptions (D22). The
    `examples` text is in `upsert_edge`'s tool description. Control: drop the `description` from one ledgered
    property (not an array's items) and build, and it raises.
  - Limits fail closed: build with one limit lowered below the measured value, and it raises.
  - The directional walker pointed at W reports a violation (pinned negative control, showing why W cannot feed
    the S walkers). The module's public entry points cover only `set_pipeline` and `upsert_node`
    (`tools/schema_contract.py:298`, `:575`), both option tools with no promoted position, so call the private
    `_directional_compatibility_failure` (`:249`) through `_schema_contract_module()`
    (`tests/unit/web/composer/test_tool_schema_contract.py:436`, which already reaches the module this way), with the
    flat S of `list_models` as the runtime schema and its strict W as the advertised one. Run it first and record
    what it returns: if it returns `None`, W's extra requiredness is not something that walker checks, so replace
    the assertion with the observed fact and say so here rather than bending the test.
  - Plugin independence is structural: `get_tool_definitions()` takes no argument and thaws a module-level registry
    (`tools/_dispatch.py:310-335`, R), and `build_wire_tool_defs` takes only its arguments. Pin that
    `_WIRE_TOOL_DEFS` equals `build_wire_tool_defs(get_tool_definitions(), limits=OPENAI_STRICT_LIMITS)`. (A
    "swap the plugin catalog and rebuild" test could never go red, so it is not written.)
  - Stamping (characterization of what T2 builds; moved here from T8): on `openai_strict`, the loop list's `strict`
    is `True` on the 32 and `False` on the 10; on `none`, no tool carries the key.
    `apply_anthropic_cache_markers` (`llm_response_parsing.py:797`) on a stamped list keeps `function.strict` and
    puts `cache_control` beside `function` on the last tool (`wire_secret_ref`, one of the 32). This is a function
    test; D8 means no production route has both. Control: a marker that rebuilt `function` without `strict` goes
    red.
  - Byte delta: record `wire_tool_definitions(OPENAI_STRICT)` for the 32 against `NONE` in the compact UTF-8 form, in
    this file's §5 (it replaces the master plan's enum-null figure).
  - `none` identity, relational (this is the durable pin): `wire_tool_definitions(NONE)` equals, with type-sensitive
    `==`, the `get_tool_definitions()` entries wrapped as `{"type":"function","function":{name,description,parameters}}`
    with the set_pipeline envelope; every tool's key set is exactly `{type, function}` and `function`'s is exactly
    `{name, description, parameters}`; and a walk asserts `type(x) is list` for every array and `type(x) is dict`
    for every object in the output. Controls: a thaw that leaves one tuple goes red (the SHA-256 alone would not,
    §2); planting `strict` on one tool goes red.
  - The SHA-256 values of §2 are checked once per commit with `tool_bytes.py` (below), as branch evidence, not as a
    durable test: an absolute hash would go red on every legitimate S edit.
- **Gates:** G-decl (no edit), G-cec, G-tier (expect no new findings from the walk if `type(...) is` is used;
  explain any that appear), G-contracts (Check 2 rows for `wire_tool_definitions:return (list)` and any other
  `dict[str, Any]` signature; census re-pin), G-skill, plus the every-task set.
  - Also run the existing byte and envelope pins, which must pass: `test_tool_schema_contract.py`,
    `test_provider_cache_markers.py` (its `parameters == definition["parameters"]` at `:253-261` is the existing
    type-sensitive round-trip check), `test_boot_probe.py`, `test_compose_loop_envelope.py`,
    `tests/unit/web/test_composer_bedrock.py` and the 10 moved files.
  - Run `tool_bytes.py`, amended to pass `NONE` and to also hash the FREEFORM and TUTORIAL_PROFILE palettes, and
    confirm all four base hashes of §2 are unchanged.
- **Commit:** `feat(composer): wire_projection — static per-dialect W with ledger, limits and faithfulness gate`.

### T3 — `wire_projection` (part 2): decode, encode, conformance

- **Add to** `wire_projection.py`:
  - `DecodedArguments`, a frozen dataclass with `semantic: Mapping[str, Any]` (deep-frozen) and `wire_conformant: bool`.
  - `decode_wire_arguments(tool_name, dialect, raw: dict[str, Any]) -> DecodedArguments`. Decorate it
    `@trust_boundary(source_param="raw", suppresses=("R1", "R5"), invariant=…, test_ref=…, test_fingerprint=…)`.
    Paste the fingerprint from the `trust_boundary.tests` rule's report; never compute it by hand. Steps:
    1. `wire_conformant` = `not any(validator.iter_errors(raw))` against the W that was sent. The per-tool
       `Draft202012Validator`s are compiled at import. This step is classification only.
    2. `EnvelopeUnwrap` if it is in the plan: `set(raw) != {"pipeline"}` or a non-dict value raises
       `ToolArgumentError(argument="set_pipeline arguments", …, category=ToolArgumentErrorCategory.WIRE_ENVELOPE)`.
       Keep the argument/expected/actual text that the deleted block produces today
       (`tool_batch.py:1000-1017`), so the planner-visible message does not change.
    3. `openai_strict` only: for each `StripNull(path)`, if the value at `path` is present and `None`, remove that
       key. Nothing else is touched. It never recurses into an option or patch map, never inserts a value, and a
       missing key stays missing.
    - Contract for names (D17): callers call it only for a tool name that was in the list sent on that call. A name
      with no W in `_WIRE_TOOL_DEFS[dialect]` raises `WireProjectionError` (a caller bug, not a model error).
  - `encode_semantic_arguments(tool_name, dialect, semantic) -> dict[str, Any]` is the inverse **of the envelope
    only** in S1 (D18): `EnvelopeUnwrap` → `{"pipeline": semantic}`, and every other tool is returned unchanged on
    both dialects. The "write `null` for an absent promoted key" branch lands in S2 with its first reader.
- **Tests (RED: the functions do not exist):** `tests/unit/web/composer/test_wire_decode.py`.
  - null becomes absent only at the 13 promoted positions. A `null` at a non-promoted position is left alone (and S
    then decides). Control: a decode that strips every `null` turns the test red.
  - An omitted wire-required key passes through unchanged, S admits it, and `wire_conformant` is `False`.
  - Decode never rejects on W failure: a raw that fails W but passes S gives `wire_conformant=False` and no exception.
    Control: a decode that raises on W failure turns the test red.
  - A `null` inside a patch map is left alone. There is no patch map on the 32 in S1, so use the `set_metadata.patch`
    nesting. `patch` declares only `name` and `description`, and both are promoted (M), so the non-promoted case must
    use an **undeclared** key: `patch.name: null` is stripped, and `patch.x_undeclared: null` is left in place (S
    then rejects it, as today). That is the equivalence property. Control: a recursing strip turns it red.
  - Decode law (decode-only, because encode's null branch is deferred, D18). Seeds: one minimal S-valid call for
    **each** of the 32, written in the test (the existing emission fixtures in `test_tool_argument_wire_parity.py`
    `_TYPE_DRIVEN_INPUTS` cover only 4 of the 32, R), plus values at the nested `set_metadata.patch.*` positions.
    Assert each seed validates against `_TOOL_SCHEMA_BY_NAME[tool]` with Draft 2020-12 before the law runs, so a bad
    seed cannot make the law vacuous. Then, with Hypothesis, vary each promoted position over three states: present
    with a value, present `null`, absent. The law: on `openai_strict`, the decoded `semantic` equals the seed with
    every `null`-valued promoted key removed, it is S-valid, and `wire_conformant` is `True` exactly when every
    promoted key is present. `hypothesis_jsonschema` is **not installed**, so the strategies are hand-rolled; add no
    dependency. Use the `ci` profile (`tests/conftest.py:156-175`).
  - Envelope round trip: `decode(encode(p))` for set_pipeline gives `p` on both dialects.
  - Unknown name: `decode_wire_arguments("not_a_tool", dialect, {})` raises `WireProjectionError` on both dialects
    (D17).
  - Presence semantics: set_pipeline `{"pipeline": {"source": {...}, "sources": null}}` through the web wire. In S1
    set_pipeline is non-strict, so decode does not strip `sources`, and S/model then decide exactly as today. Pin the
    current outcome: admitted through the web wire if and only if it is admitted flat through MCP. The master plan's
    "accepted via web wire" case belongs to S2, where `sources` is promoted. Say so in the test docstring.
  - Envelope: a valid enveloped set_pipeline gives `wire_conformant=True`; its unwrapped form validated against
    the same W gives `False` (control); `{}`, `{"pipeline": 1}` and `{"pipeline": {...}, "x": 1}` each raise
    `WIRE_ENVELOPE`.
  - Tri-state check for S1: on the 2 already-nullable positions, the handler result is identical for `null` and
    for absent (`upsert_edge` with `label: null` and without `label`; `get_plugin_assistance` with `issue_code: null`
    and without it). This is the master plan's §3.4 premise that `null` has no distinct S meaning at any promoted
    position.
- **Extend** `tests/unit/web/composer/test_error_class_producer_census.py` `_produced_arg_error_classes`
  (`:344-374`), the "every ARG_ERROR producer" census, with the new producer:
  `_raised_class_name(lambda: decode_wire_arguments("set_pipeline", ToolContractDialect.NONE, {}))`. It yields
  `ToolArgumentError`, which is already allowlisted, so `_SAFE_ARG_ERROR_CLASSES` does not change.
- **Gates:** G-tier (the new `R_TB_SUPPRESSED` lines for `decode_wire_arguments` are expected additions, so list
  them in the commit message), `trust_boundary.*` rules, G-cec, G-census-S0, G-contracts (Check 2 rows for
  `decode_wire_arguments` and `encode_semantic_arguments`; census re-pin, with the boundary column moving by the
  decorated parameter), plus the every-task set.
- **Commit:** `feat(composer): wire decode/encode — STRIP_NULL, envelope unwrap, wire_conformant classification`.

### T4 — `strict_transport.py`, the setting, and env docs

- **Create** `src/elspeth/web/composer/strict_transport.py`. It sits outside `tools/`, and its top-level imports are
  only stdlib and `elspeth.contracts` (for `ToolContractDialect`), so `web/config.py` can import its `Literal` the
  way it imports `ReasoningEffort` (`config.py:37` → `reasoning.py:23`). Heavy imports (LiteLLM,
  `llm_response_parsing`) go inside the function.
  - `StrictToolsSetting = Literal["preferred", "forward_to_endpoint", "off"]`.
  - `StrictTransport(StrEnum)`: `ENFORCING`, `FORWARDING`, `NONE`. `dialect_for(transport) -> ToolContractDialect`
    maps `NONE` to `NONE` and the other two to `OPENAI_STRICT`.
  - `StrictTransportDiagnostic(StrEnum)`, closed (D24): `UNPARSEABLE_OPENROUTER_API_BASE`,
    `UNPARSEABLE_OPENAI_BASE_URL`, `UNPARSEABLE_OPENAI_API_BASE`. `StrictTransportResolution` is a frozen dataclass
    `(transport: StrictTransport, diagnostic: StrictTransportDiagnostic | None)`; `diagnostic` is set only by row 3a.
  - `resolve_strict_transport(*, model, api_base, setting, env: Mapping[str, str]) -> StrictTransportResolution`
    applies the table below. The first matching row wins. It never raises for env input (D24).

| # | Condition | `preferred` | `forward_to_endpoint` | `off` |
|---|---|---|---|---|
| 1 | setting is `off` | — | — | `NONE` |
| 2 | `supports_anthropic_prompt_cache_markers(model)` (D8) | `NONE` | `NONE` | `NONE` |
| 3 | `litellm.get_llm_provider(model=model, api_base=api_base)` raises `litellm.exceptions.BadRequestError`, or its provider is not an exact `str` | `NONE` | `NONE` | `NONE` |
| 3a | provider `openrouter` or `openai`, `api_base` is `None`, and the env base the row 4/6 selection would use makes `urlsplit` raise `ValueError` or gives `hostname is None` (D24; diagnostic names the variable) | `NONE` | `NONE` | `NONE` |
| 4 | provider `openrouter`; base = `api_base` or `env["OPENROUTER_API_BASE"]` or `https://openrouter.ai/api/v1`; host is `openrouter.ai` | `FORWARDING` | `FORWARDING` | `NONE` |
| 5 | provider `openrouter`, any other host (C12) | `NONE` | `FORWARDING` | `NONE` |
| 6 | provider `openai`; base = `api_base` or `env["OPENAI_BASE_URL"]` or `env["OPENAI_API_BASE"]` or `https://api.openai.com/v1`; host is `openrouter.ai` | `FORWARDING` | `FORWARDING` | `NONE` |
| 7 | provider `openai`, host is `api.openai.com` or ends with `.api.openai.com` | `NONE` (ruling 7) | `ENFORCING` | `NONE` |
| 8 | provider `openai`, any other host (ELSPETH's gateway, a proxy, an env-set base) | `NONE` | `FORWARDING` | `NONE` |
| 9 | provider `azure`; version = `env["AZURE_API_VERSION"]` or `_LITELLM_AZURE_DEFAULT_API_VERSION` (`"2025-02-01-preview"`, pinned); version is `preview`/`latest`/`v1` or an ISO date ≥ `2024-08-01` (a `-preview` suffix allowed) (C11) | `NONE` (ruling 7) | `ENFORCING` | `NONE` |
| 10 | provider `azure`, older or unparseable version | `NONE` | `NONE` | `NONE` |
| 11 | any other provider (`anthropic`, `bedrock`, `azure_ai`, `vertex_ai`, `deepseek`, `hosted_vllm`, …) | `NONE` | `NONE` | `NONE` |

  - **Ruling 7 (lead, provisional; John may overrule), option (a):** under `preferred` nothing resolves to
    `ENFORCING`; the only non-`NONE` rows under the default are 4 and 6, both OpenRouter hosts. Hosted OpenAI (row 7)
    and Azure (row 9) are `ENFORCING` only under `forward_to_endpoint`, until R8 has measured them live. Reverting the
    ruling is a two-cell table change plus the T4/T8/T10/T11 expectations that name it.
  - Hosts come from `urllib.parse.urlsplit(base).hostname`, parsed as Tier-3 input: the `ValueError` is caught at
    that one call and turned into row 3a. A settings base cannot reach row 3a, because settings already reject
    malformed URLs, query strings and fragments (`config.py:145-175`, R).
  - Row 7 is deliberately narrower than LiteLLM's own hosted-OpenAI test, which accepts `openai.com` and any
    `*.openai.com` (`gpt_transformation.py:407-419`). Other `openai.com` hosts fall to row 8 and resolve to `NONE`
    under `preferred`, which is today's bytes.
  - The two-route helper `resolve_composer_tool_contract` (D20) is **not** added here; it lands in T8 with its first
    two callers.
  - The `get_llm_provider` call is the Tier-3 site. Put it in one private helper,
    `_routing_provider(model, api_base) -> str | None`, which the resolver calls and nothing else duplicates. Read
    only index 1 of its tuple, discard the rest (index 2 can be an env-read key), and check `type(provider) is str`
    (otherwise `None`, row 3). Catch only `BadRequestError`. Anything else propagates, because a crash in LiteLLM's
    resolver is a LiteLLM bug.
  - On a miss, LiteLLM prints a provider banner to stdout (observed). That is acceptable, because it happens only
    for unknown bare names, which availability already marks unusable (`availability.py:94-108`).
- **Modify** `src/elspeth/web/config.py`: add `composer_strict_tools: StrictToolsSetting = "preferred"` next to
  `composer_boot_probe_enabled` (`:307`), with a field description.
- **Modify** `src/elspeth/web/composer/protocol.py` `ComposerSettings` (`:1429`): add
  `composer_strict_tools: StrictToolsSetting` so the probe and the service can read it through the Protocol.
  - Structural test doubles do not name the Protocol, so `grep -rln ComposerSettings tests` finds none of them.
    Inventory the settings doubles passed to `build_composer_probe_requests`, `build_composer_loop_request_kwargs`
    and `ComposerServiceImpl`: `grep -rln "SimpleNamespace(" tests/unit/web/composer | xargs grep -ln composer_model`
    returns `test_planner_authoring_aids.py`, `test_boot_probe.py`, `test_compose_loop_llm_audit.py` and two guided
    files, and those hits are a starting point, not the full list.
  - Each double that reaches the T8 resolver needs `composer_strict_tools`, or the read raises. Add the attribute
    in T8, where it is first read.
- **Modify** `docs/reference/environment-variables.md`:
  - add an `ELSPETH_WEB__COMPOSER_STRICT_TOOLS` row or paragraph in the composer section (`:240-346`), giving the 3
    values, the default and the resolution rules in plain words: under `preferred` only OpenRouter routes send
    `strict`, and custom endpoints, hosted OpenAI and Azure send today's bytes; `forward_to_endpoint` for
    operator-verified gateways and as the opt-in for hosted OpenAI and Azure, noting that ELSPETH's own gateway
    rejects any `strict` key, `false` included; `off` as the remedy; a malformed `OPENAI_BASE_URL`,
    `OPENAI_API_BASE` or `OPENROUTER_API_BASE` makes that route send today's bytes (D24). Two more plain sentences land
    with the tasks that make them true:
    - (with T9) `off` restores the tool wire and turns off decode; it does not revert the `validation_errors` in
      tool results, which are independent of the setting;
    - (with T8) under `forward_to_endpoint`, hosted OpenAI (bare `gpt-5.5` with no endpoint, the Azure Container Apps
      bicep default) and Azure routes with an api-version of at least `2024-08-01` resolve to `enforcing`, which no
      live endpoint has yet accepted from ELSPETH; a planner-route rejection at boot stops the app, so the first boot
      after opting in is that route's live acceptance test, and `preferred` or `off` is the remedy (§7.3 item 7). A
      rejection of the conditional `hatch_terminal` request is logged and does not stop boot (§7.3 item 8);
  - rewrite the boot-probe paragraph (`:268-282`) to mention the strict counts and the conditional, non-fatal
    `hatch_terminal` request (16 `max_tokens`, a rejection check only) (the text lands with T8; the setting row lands
    here).
- **Tests (RED):** `tests/unit/web/composer/test_strict_transport.py`.
  - Each row below also carries a **literal** expected LiteLLM provider, copied from `$L/provider_matrix.log`, and
    the test asserts `_routing_provider(model, api_base)` equals that literal (or is `None` for the unknown names,
    where LiteLLM raises `BadRequestError`). This is the drift pin: comparing the resolver's provider with
    `get_llm_provider` itself would be a tautology, because the resolver *is* that call; a literal is not. The literals:
    `gpt-5.5` (no base, loopback, env base) → `openai`; `openrouter/…` (every row, including `openrouter/auto` and the
    proxy base) → `openrouter`; `openai/…` + an openrouter base → `openai`; `anthropic/…` → `anthropic`; bare
    `claude-sonnet-4-6` + loopback → `anthropic`; `gpt-5.5` + `https://api.deepseek.com/v1` → `deepseek`; `azure/…` →
    `azure`; `azure_ai/…` → `azure_ai`; `bedrock/…` → `bedrock`; `probe-model`, `m` → raises. The Anthropic rows are
    resolved to `NONE` by row 2 before the provider is read, so this literal column is the only place their LiteLLM
    provider is pinned.
    - Mutation controls (Codex finding 6: they mutate the thing under test, never the expected literal, which would
      only prove the assert runs): (i) mutate the routing input while keeping the literal: on the `deepseek` row,
      swap `https://api.deepseek.com/v1` for `https://api.openai.com/v1` and that row goes red (LiteLLM then says
      `openai`); (ii) mutate the resolution result: `monkeypatch.setattr` `litellm.get_llm_provider` with a wrapper
      that returns `"openai"` in index 1 for `openrouter/` models, and every `openrouter` row goes red. Record both
      logs.
  - A parametrised expected-values table for `preferred` (each case asserts the whole `StrictTransportResolution`,
    diagnostic included), covering:
    - default planner `gpt-5.5` with no base → `NONE` (row 7 under ruling 7);
    - default hatch `anthropic/claude-sonnet-4-6` → `NONE`;
    - deployed `openrouter/deepseek/deepseek-v4.1-flash` → `FORWARDING`;
    - deployed hatch `openrouter/z-ai/glm-5.3` → `FORWARDING`;
    - the gateway test `gpt-5.5` + `http://127.0.0.1:8787/v1` → `NONE`;
    - env gateway `gpt-5.5` with `OPENAI_BASE_URL=https://gw.example/v1` → `NONE`;
    - `openai/deepseek/deepseek-v4.1-flash` + `https://openrouter.ai/api/v1` → `FORWARDING`;
    - `openrouter/anthropic/claude-sonnet-4-6` → `NONE`;
    - `openai/anthropic/claude-sonnet-4-6` + an openrouter base → `NONE` (D8);
    - bare `claude-sonnet-4-6` + loopback → `NONE`;
    - `gpt-5.5` + `https://api.deepseek.com/v1` → `NONE` (C1);
    - `azure/gpt-4.1` with env unset → `NONE` (row 9 under ruling 7);
    - `azure/gpt-4.1` with `AZURE_API_VERSION=2024-06-01` → `NONE` (row 10);
    - `azure/gpt-4.1` with `AZURE_API_VERSION=preview` → `NONE` (row 9 under ruling 7);
    - `azure_ai/x` → `NONE`;
    - `bedrock/anthropic.claude-…` → `NONE`;
    - unknown bare names `probe-model` and `m` → `NONE`;
    - `openrouter/auto` → `FORWARDING` (D7);
    - `openrouter/deepseek/…` + `https://proxy.example/v1` → `NONE`;
    - env-selected OpenRouter proxy: `openrouter/deepseek/…` with `OPENROUTER_API_BASE=https://proxy.example/v1` →
      `NONE` (row 5), diagnostic `None`;
    - **malformed env bases (D24, Codex finding 1):** `openrouter/deepseek/…` with `OPENROUTER_API_BASE=http://[` →
      `NONE`, diagnostic `UNPARSEABLE_OPENROUTER_API_BASE`; `gpt-5.5` with `OPENAI_BASE_URL=http://[` → `NONE`,
      `UNPARSEABLE_OPENAI_BASE_URL`; `gpt-5.5` with `OPENAI_API_BASE=not-a-url` (hostname `None`) → `NONE`,
      `UNPARSEABLE_OPENAI_API_BASE`. None of these raises. Control: remove the `ValueError` catch and the first case
      raises `ValueError: Invalid IPv6 URL` (the plan's original parse, M `$L/plan-review/env_url_probe.log`).
  - The same table under `forward_to_endpoint`: rows 5 and 8 flip to `FORWARDING`, rows 7 and 9 flip to `ENFORCING`
    (so `gpt-5.5` no base, `azure/gpt-4.1` env unset and `AZURE_API_VERSION=preview` are `ENFORCING`), and nothing
    else moves; the malformed-env cases stay `NONE` with their diagnostics. Control: making `forward_to_endpoint` widen
    row 3, row 3a or row 11 turns it red.
  - Under `preferred`, no case in the table resolves to `ENFORCING` (ruling 7). Control: restore the pre-ruling row 7
    cell (`ENFORCING` under `preferred`) and the `gpt-5.5` no-base case goes red.
  - Under `off`, every row → `NONE`.
  - Pins against LiteLLM:
    - `_LITELLM_AZURE_DEFAULT_API_VERSION == litellm.AZURE_DEFAULT_API_VERSION`;
    - the literal provider column above. `get_llm_provider` reads the real `os.environ`, not the injected `env`, so
      these tests `monkeypatch.delenv` every `OPENAI_*`, `OPENROUTER_*`, `AZURE_*` and `ANTHROPIC_*` variable (with
      `raising=False`), which keeps LiteLLM's answer hermetic.
    - A `get_llm_provider` call was seen to hang past 120 s in one review session (unexplained; the lane's
      `provider_matrix.log` run completed). Run this file with a `timeout` wrapper the first time and record the
      wall time.
  - ELSPETH never sets LiteLLM's globals: an AST census over `iter_gate_sources` for `src/elspeth` finds no
    assignment target `litellm.api_base` / `litellm.api_version`. Control: a planted assignment in a synthetic
    source string is found.
  - Settings: `WebSettings().composer_strict_tools == "preferred"`; `ELSPETH_WEB__COMPOSER_STRICT_TOOLS=off` loads
    through `settings_from_env`; an invalid value is rejected by pydantic.
- **Gates:** G-contracts (the `Mapping[str, str]` env parameter is not counted), G-attr (no `getattr` on `litellm`;
  use plain attribute access), plus the every-task set, `tests/unit/web/test_config.py`,
  `tests/unit/deployment/test_web_settings_exports_resolve.py` (bites only if a tracked deploy file exports the name,
  and S1 exports it nowhere), `tests/unit/docs` (the env-var doc edit), G-tier.
- **Commit:** `feat(composer): strict_transport resolver and composer_strict_tools setting (default preferred)`.

### T5 — Audit contract fields (the wire is still unchanged)

- **Modify** `src/elspeth/contracts/composer_llm_audit.py`:
  - `ComposerLLMCall` gains `tool_contract_dialect: ToolContractDialect | None = None` and
    `strict_tool_count: int | None = None`.
  - `__post_init__` checks:
    - both are `None`, or both are set;
    - `type(strict_tool_count) is int` and `>= 0`;
    - `dialect is NONE ⇒ strict_tool_count == 0`;
    - the dialect is the owned enum.
  - `to_dict` writes `tool_contract_dialect` as `.value`, like `status` at `:305`, because `deep_thaw` leaves the
    enum instance in place.
- **Modify** `src/elspeth/web/composer/llm_response_parsing.py` `build_llm_call_record` (`:653`). Derive both fields
  from the `tools` argument (D11):
  - `tools is None` or empty → `None`/`None`;
  - every `tool["function"]` has a key `strict` whose value is an exact `bool` → `OPENAI_STRICT` and
    `count(strict is True)`;
  - no tool has the key → `NONE`, `0`;
  - anything else → `AuditIntegrityError("sent tool list mixes strict-stamped and unstamped tools")`.
  - Keep the function name, because it is in `_R5_NAMED_BOUNDARY_CONTEXTS`.
- **Modify** `src/elspeth/web/composer/audit.py` `_LLM_CALL_PUBLIC_AUDIT_FIELDS` (`:342-373`): append
  `"tool_contract_dialect"` and `"strict_tool_count"`.
- **Modify** `src/elspeth/contracts/composer_audit.py` `ComposerToolInvocation`:
  - add `strict_sent: bool | None = None` and `wire_conformant: bool | None = None`, with the meanings of D16 and C23;
  - in `__post_init__`, each is `None` or an exact `bool`. There is no implication between them: on the `none`
    dialect a decoded call has `strict_sent=None` and a boolean `wire_conformant`.
- **Modify** `src/elspeth/web/composer/audit.py` `DispatchAudit` (`:563`):
  - add the same two fields, default `None`;
  - `begin_dispatch` (`:591`) and `begin_dispatch_or_arg_error` (`:638`) take them as keyword arguments, default
    `None`, which covers `pipeline_commit.py:514`, guided (`guided/_discovery.py:50`, `:221`) and the 25 test call
    sites. Every site that knows the facts passes them explicitly (D21: T6 and T7);
  - the four `finish_*` builders (`:710`, `:791`, `:843`, `:879`) and `dispatch_with_audit` (`:950`) copy them.
    `rebind_dispatch_arguments` uses `dataclasses.replace`, so it carries them.
- **Tests (RED):**
  - `tests/unit/web/composer/test_llm_tool_contract_audit.py`, modelled on `test_llm_provider_served_audit.py`:
    - `TestDeclaredFields`;
    - contract checks (both-or-neither, negative count, `NONE` with a count > 0, wrong type);
    - derivation: a stamped list → (`openai_strict`, count), an unstamped list → (`none`, 0), a mixed list →
      `AuditIntegrityError`, `None` → (`None`, `None`);
    - `TestSurvivesToThePersistedProjection`: `llm_call_audit_envelope(call)["call"]` has both keys, `None` persists
      as `null` rather than being omitted, and `to_dict()["tool_contract_dialect"]` is a `str`. **Mutation control:**
      monkeypatch `_LLM_CALL_PUBLIC_AUDIT_FIELDS` to the tuple without each key, and the test goes red (the same
      technique as `test_llm_provider_served_audit.py:256-264`);
    - a guided failure-row projection keeps both (`sessions/guided_audit.py:195-240`).
  - `tests/unit/contracts/test_composer_audit.py` (additions): shape and exact-`bool` type checks for the two
    invocation fields (including `strict_sent=None` with `wire_conformant=False`, which is valid), and `to_dict`
    carries them.
  - `tests/unit/web/composer/test_dispatch_audit_wire_facts.py`: every `finish_*` path and `dispatch_with_audit`
    copies the facts from `DispatchAudit`. Control: drop the copy in one builder and it goes red.
- **Gates:** G-census-S0 (the construction-site census must stay at exactly one site), G-contracts (Check 1: the
  enum lives in `contracts/`), plus the every-task set. Also run `tests/unit/composer_mcp/test_call_tool_audit.py`: MCP builds
  `ComposerToolInvocation(...)` at `composer_mcp/server.py:980` and takes the `None` defaults, which is honest because
  no W is sent there.
- **Commit:** `feat(audit): tool_contract_dialect/strict_tool_count on LLM calls, strict_sent/wire_conformant on invocations`.

### T6 — Decode in the compose loop, with every route still on `none`

- **Modify** `src/elspeth/web/composer/tool_batch.py`:
  - `ToolBatchContext` (`:623`) gains a required `tool_contract_dialect: ToolContractDialect`. It is built once at
    `service.py:5652` from `self._planner_dialect`, which `ComposerServiceImpl.__init__` sets to
    `ToolContractDialect.NONE` in this commit. That is the single resolution point, and T8 replaces it. The loop
    list call (`service.py:7161`) switches from T2's literal `NONE` to `self._planner_dialect`, so the list that is
    sent and the dialect that decode uses always come from the same value.
  - Per call, at the top of the `for tool_call in assistant_tool_calls` loop (`:812`), create one mutable
    `_CallWireFacts(strict_sent: bool | None = None, wire_conformant: bool | None = None)`. Capture it in
    `_append_tool_outcome` as a default argument (`_wire=call_wire_facts`), alongside `_pre_version` (`:849`).
    Decode runs after the closure is defined, so late binding through a loop variable would trip ruff B023, and a
    captured holder avoids that.
  - The compose loop always sends all 42 tools, so its sent set is exactly the keys of
    `_WIRE_TOOL_DEFS[dialect]`. A name outside it (a hallucinated or unknown tool, `:826-838`) skips decode entirely
    (D17): its arguments flow on as today, with `strict_sent=None` and `wire_conformant=None`.
  - `strict_sent` (D16) is set as soon as the tool name is known to be in the sent set: on `OPENAI_STRICT` it is
    `_WIRE_TOOL_DEFS[dialect][name].strict_capable` (`True` on the 32, `False` on the 10); on `NONE` it stays `None`
    (no key was sent). It stays `None` for an unknown tool.
  - JSON decode failure (`:863-931`): `wire_conformant` stays `None` (no value).
  - Non-object (`:938-993`): `wire_conformant = False`.
  - **Replace the envelope block `:995-1045`** with (for a name in the sent set):
    `try: decoded = decode_wire_arguments(tool_name, ctx.tool_contract_dialect, decoded_arguments)`
    `except ToolArgumentError as envelope_rejection:` followed by the four side effects the block has today, in the
    same order:
    - `turn_has_mutation = True` (`:998`);
    - sentinel audit arguments;
    - the `decoded_args_by_call_id` entry (`:1004`);
    - the transcript rewrite through `_replace_llm_tool_call_arguments` (`:1005`, still re-enveloping the sentinel
      for set_pipeline).
    - Then the ARG_ERROR outcome with `error_class=type(envelope_rejection).__name__`,
      `error_category=envelope_rejection.category` and `wire_conformant = False`.
    - On success: `arguments = cast(dict[str, Any], deep_thaw(decoded.semantic))` and
      `wire_conformant = decoded.wire_conformant`.
    - **Keep the name `arguments`.** The wire census binds `ctx.service._validate_advisor_arguments(arguments)` by
      that name (`composer_wire_census.py:406-437`). Its meaning changes: `arguments` is now the decoded (semantic)
      form, so the advisor validator sees `request_advisor_hint.schema_excerpt: null` as absent. That is intended (S
      is the contract), but the census's "complete original arguments" wording would now be wrong. Update the census
      docstring and its failure message to say "the decoded arguments" (no logic change), and name the change in
      the commit message.
  - Pass `strict_sent`/`wire_conformant` explicitly at **all four** pre-dispatch sites (D21): `begin_dispatch` at
    `:891` (JSON failure: known `strict_sent`, `None`), `begin_dispatch_or_arg_error` at `:948` (non-object: known
    `strict_sent`, `False`), `begin_dispatch` at `:1010` (envelope rejection: known `strict_sent`, `False`) and
    `begin_dispatch_or_arg_error` at `:1061` (main path). Every invocation built from those `DispatchAudit`s then
    carries the same facts as the `_ToolOutcome`. On paths where P4 did not persist, the route drain stores these
    invocations (C2), so a `None` default here would store a different value from the P4 row for the same call.
  - `_replace_llm_tool_call_arguments` (`:450`) is module-level and today takes only `llm_messages` and the
    keyword-only `tool_call_id`/`arguments` (R); no `ctx` is in scope inside it (Codex finding 5). Give it two
    **required** keyword-only parameters, `dialect: ToolContractDialect` and `semantic: bool`, so both choices are
    explicit at each call site: `_replace_llm_tool_call_arguments(llm_messages, *, tool_call_id, arguments, dialect,
    semantic)`.
    - The set_pipeline branch (`:481`) calls `encode_semantic_arguments("set_pipeline", dialect, arguments)` **only
      when `semantic` is true**, which is the `:1069`, `:1302`, `:1404` and `:1438` callers. The sentinel callers
      (`:834`, `:886`, `:1005`) keep today's bytes: flat for other tools, and re-enveloped for set_pipeline by the
      same `{"pipeline": …}` wrap.
    - All 7 production callers sit inside `run_tool_batch` (`:686`; no other `def` between it and `:1450`, R), so
      each passes `dialect=ctx.tool_contract_dialect`. Both direct test callers
      (`tests/unit/web/composer/test_dispatch_arms_characterization.py:160`, `:189`) gain `dialect=` and `semantic=`.
    - In S1 both branches produce identical bytes for set_pipeline, because set_pipeline is non-strict on both
      dialects. Pin it for **both** dialects: for `NONE` and `OPENAI_STRICT`, the transcript bytes from
      `semantic=True` and `semantic=False` are byte-identical for a set_pipeline call, and the sentinel bytes for
      set_pipeline and `list_models` equal today's output (`dispatch_probe.log` item 1). Control: make the semantic
      branch skip the envelope and the set_pipeline pin goes red on both dialects.
- **Modify** `src/elspeth/web/composer/_compose_loop_carriers.py` `_ToolOutcome` (`:187`): add
  `strict_sent: bool | None = None` and `wire_conformant: bool | None = None`, and extend its cross-check to exact
  `bool` or `None` (no implication between them, D16). The defaults keep the 5 existing test constructions
  (`test_compose_loop_carriers.py:128`; `tests/unit/web/sessions/test_messages_route_rejection_reasons.py:66`, `:80`,
  `:92`, `:119`) unchanged; `run_tool_batch` always passes both, and the parity pin below checks it.
- **Modify** `src/elspeth/web/composer/turn_audit.py` (`:230-240`, D1): each redacted assistant `tool_calls` entry
  becomes `{"id", "type", "function", "strict_sent", "wire_conformant"}`, taken from the matching `_ToolOutcome`.
  The two keys are always present (`null` when not applicable).
  - Before editing, re-run the reader trace for persisted assistant `tool_calls` entries and save it in `$L`:
    - `sessions/routes/_helpers.py:1600-1611` (`_kind` membership) and `sessions/schemas.py:163-164`
      (`ToolCallObject = dict[str, JsonValue]`);
    - the frontend `ToolCall` readers (`grep -rln tool_calls src/elspeth/web/frontend/src`; `ToolCall` in
      `frontend/src/types/index.ts:90-105` is an interface with no runtime decoder, R);
    - `_composer_chat_history` (it does not copy `tool_calls`).
    - Control: the trace must find the one real exact key-set reader of persisted `tool_calls`,
      `composer/control_messages.py:105` (`frozenset(envelope) != _CONTROL_ENVELOPE_KEYS`). It applies only to
      entries whose `_kind` is the control kind (`:100-102`), and assistant entries carry no `_kind`, so D1 does not
      reach it. A trace that misses it is not trusted. (`_helpers.py:1611` is a `_kind` membership check, not a
      key-set comparison, so it is not a valid control.)
    - The write side was read while this plan was written (R, confirm again):
      - `chat_messages.tool_calls` is a plain nullable `JSON` column with no CHECK on its content
        (`sessions/models.py:703`);
      - `_persist_payload.py:117-129` only deep-freezes `tool_calls` (no shape gate);
      - `add_messages_atomic` deep-thaws it before the write.
  - If any reader or writer compares the entry's key set exactly, stop (§7.2). That would mean an epoch bump, and
    §6 step 4's "no testcontainer" would no longer hold.
- **Tests (RED):** `tests/unit/web/composer/test_compose_loop_wire_decode.py`.
  - How the tests reach `OPENAI_STRICT` in T6. No test constructs `ToolBatchContext` directly (M: `grep -rn
    "ToolBatchContext(" tests` finds nothing). Tests reach `run_tool_batch` through `service._dispatch_tool_batch`
    (e.g. `test_compose_loop_carriers.py:237`). So until T8, set `service._planner_dialect =
    ToolContractDialect.OPENAI_STRICT` on the constructed service in the new test file. T8 replaces those
    assignments with settings that resolve to the dialect.
  - A compose turn on `OPENAI_STRICT` where the model sends
    `list_models` with `{"provider": null, "limit": null}` is admitted. The handler receives `{}`, and the persisted
    assistant entry has `strict_sent: true, wire_conformant: true`.
    - RED today: S rejects `null` at the omission-only `provider`, so the outcome is ARG_ERROR `schema_shape`.
    - Control: skip decode and it goes ARG_ERROR again.
  - The same call with the keys omitted is admitted, and `wire_conformant: false`.
  - On `NONE`, `list_models {"provider": null}` is still rejected by S exactly as today (no strip on `none`), and
    `strict_sent: null` (no key was sent, D16).
  - On `OPENAI_STRICT`, an option tool such as `upsert_node` records `strict_sent: false`.
  - An unknown tool name on `OPENAI_STRICT` never reaches decode (control: route it into `decode_wire_arguments` and
    the test goes red with `WireProjectionError`).
  - Parity pin (D21): for the SUCCESS, non-object, envelope-rejection, JSON-failure and unknown-tool branches,
    each recorded `ComposerToolInvocation`'s `(strict_sent, wire_conformant)` equals the pair on the persisted P4
    assistant entry for the same call id (which comes from `_ToolOutcome`). Use whichever handle the existing carrier
    tests use to read the recorder's invocations (`recorder.invocations` feeds `tool_invocations` at
    `tool_batch.py:2176`, `:2223`). Control: drop the explicit facts at `:948` and it goes red.
  - The envelope pins keep their assertions unchanged: `test_dispatch_arms_characterization.py:195-~250` (4 invalid
    envelopes → `ToolArgumentError`/`WIRE_ENVELOPE`, sentinel `arguments_canonical`, handler never reached) and
    `:137-167` (transcript re-envelope). The only edits in that file are the `dialect=` and `semantic=` keywords at
    the two direct calls (`:160`, `:189`); the two-dialect byte pins go in the new file.
  - The sentinel transcript bytes for set_pipeline and for `list_models` are pinned byte-for-byte to today's output
    (`dispatch_probe.log` item 1 gives the expected strings).
  - The unknown-tool and JSON-decode-failure rows give `strict_sent`/`wire_conformant` = (`None`, `None`) and
    (the D16 value for that tool and dialect, `None`).
  - The persisted P4 row carries the two keys on SUCCESS, ARG_ERROR and PLUGIN_CRASH. Extend a copy of the canary
    `test_compose_loop_records_success_arg_error_plugin_crash_sequence` in a new file. Control: drop the keys in
    `turn_audit` and it goes red.
- **Gates:**
  - G-wire (must stay green; a red here means `arguments` was renamed);
  - G-census-S0: `test_error_class_producer_census.py`, where the envelope writer is now `type(envelope_rejection)`,
    a caught exception, and `_REVIEWED_ORIGINS` liveness must still hold;
  - G-tier (the deleted block and the moved code may stale signed `scope_fingerprint`s in `tool_batch.py`; list them
    for the operator and do not stage anything);
  - the every-task set;
  - `tests/unit/scripts/test_composer_wire_census.py` (the census docstring/message edit);
  - the whole of `tests/unit/web/composer` and `tests/unit/web/sessions` at the default worker count, because P4 row
    shapes move. **Expected pin moves:** exact-dict assertions on persisted assistant `tool_calls` entries (for
    example in `test_compose_loop_persistence.py`). Each move adds the two keys and nothing else. Attribute every
    other red.
  - `tool_bytes.py` must still show all four base hashes (no wire change in T6).
- **Commit:** `feat(composer): decode provider tool arguments through wire_projection; record wire facts on P4 rows`.

### T7 — The pipeline planner: dialect plumbing, hoisted endpoint, discovery decode (still `none`)

- **Modify** `src/elspeth/web/composer/pipeline_planner.py`:
  - `PlannerModelConfig` (`:675`) gains two **required** fields: `tool_contract_dialect: ToolContractDialect` and
    `escape_hatch_tool_contract_dialect: ToolContractDialect`. The 3 service construction sites (`service.py:4425`,
    `:4837`, `:5344`) pass `self._planner_dialect` and `self._hatch_dialect`, both `NONE` in this commit. The two test
    constructions (`tests/unit/web/composer/test_pipeline_planner.py` `_model()` helper, and
    `tests/integration/web/composer/guided/test_proposal_audit_projection.py`) pass `NONE`.
  - `planner_tool_definitions(policy=None, *, dialect, terminal_contract=None)` (`:1550`): the discovery entries come
    from `wire_tool_definitions(dialect)` filtered and ordered by `PLANNER_DISCOVERY_TOOL_NAMES`/policy, not from
    `get_tool_definitions()` (`:1556`). The terminal comes from `planner_terminal_tool_definition(terminal_contract, dialect=dialect)`.
  - `planner_terminal_tool_definition(terminal_contract=None, *, dialect)` (`:1485`) returns
    `stamp_planner_terminal(…, dialect)`. `parameters` is unchanged, so `capability_skill.py:244-249`'s terminal
    schema hash does not move.
  - Callers: `:3775` and `:5217` pass `model_config.tool_contract_dialect`. The hatch (`:4260`) passes
    `model_config.escape_hatch_tool_contract_dialect`.
  - `call_model` (`:3856`):
    - Hoist the api_base/api_key choice (`:3972-3978`) to just after `effective_model` (`:3866`), using the same
      condition (`model_override is not None`).
    - Select `effective_dialect` by that same condition.
    - Before `apply_anthropic_cache_markers` (`:3869`), assert that `active_tools` is stamped for `effective_dialect`:
      every tool has an exact-`bool` `strict` if and only if the dialect is `OPENAI_STRICT`, or
      `AuditIntegrityError`. The manifest and `tools_spec_hash` then cover the stamped bytes (C4).
    - The attempt loop uses the hoisted values.
  - Discovery decode (C3, D17). `_parse_response_tool_calls` (`:1682`) gains two keywords: `dialect` and
    `sent_tool_names: frozenset[str]`, the names in `active_tools` for that call. For each non-terminal call whose
    name is in `sent_tool_names`, it runs `decode_wire_arguments(name, dialect, parsed_object)` before building
    `_ParsedToolCall` (`:1738`).
    - `_ParsedToolCall` (`:1414`) gains `strict_sent: bool | None = None` and `wire_conformant: bool | None = None`
      (both `None` for the terminal). The defaults keep the 17 existing test constructions unchanged (15 in
      `test_pipeline_planner.py`, 2 in `test_discovery_response_contracts.py:173`, `:198`).
    - `strict_sent` follows D16 for the stamp that was sent on that call.
    - The call site at `:4129` passes `effective_dialect` and the names of the hoisted `active_tools`.
    - A discovery name that is not in the sent palette is **not** rejected today: the palette is not enforced at
      dispatch (C21). S1 keeps that path as it is and only skips decode for it: arguments unchanged,
      `strict_sent=None`, `wire_conformant=None`. (Example: `get_pipeline_state`, never in a production palette,
      with `component: null` stays `null` and S rejects it, exactly as today.)
    - This route has no envelope; set_pipeline is not a discovery tool.
  - `execute_one_discovery` (`:5030`): `begin_dispatch(...)` (`:5033`) passes `call.strict_sent`/`call.wire_conformant`.
    The planner invocations persist whole through `service.py:4949`, so the fields reach storage with no further
    change.
  - Transcript: `_assistant_tool_calls_message` (`:1856`) replays `raw_arguments` verbatim. There is no encode on this
    route; the transcript already holds the wire form.
- **Modify** `boot_probe.py:166`: `planner_tool_definitions(dialect=ToolContractDialect.NONE)` in this commit (T8
  resolves it). Update the planner-list callers in the 7 test files the same way (mechanical).
  - One of them is not a plain call: `tests/integration/web/composer/parity/test_schema_mutation_controls.py:262-269`
    monkeypatches `planner_terminal_tool_definition` with `_narrowed_terminal(terminal_contract=None)`. After this
    task `planner_tool_definitions` calls it with `dialect=`, so the replacement must accept and forward it
    (`_narrowed_terminal(terminal_contract=None, *, dialect)`), or the test fails with `TypeError` instead of its
    asserted `AuditIntegrityError`. Its assertions do not change.
- **Tests (RED):** `tests/unit/web/composer/test_planner_strict_dialect.py`. This is a new file, kept out of
  `test_pipeline_planner.py` (merge-order note).
  - On `OPENAI_STRICT`, a scripted planner turn whose discovery call is `list_models {"provider": null, "limit": null}`
    reaches dispatch with `{}`. `_tool_information_keys` and the cycle-guard hash see the semantic form (assert that
    `stable_hash(call.arguments)` equals the hash of `{}`), and the persisted planner invocation envelope carries
    `strict_sent: true, wire_conformant: true`.
    - RED today: the test constructs `PlannerModelConfig` with the two new fields, so it first fails with a
      `TypeError` at construction. Record that as the RED reason. Once the fields exist but before decode is
      wired, it fails because S rejects the `null`; record that too.
    - Control: skip decode in `_parse_response_tool_calls` and it goes red.
  - Unsent name: on `OPENAI_STRICT` with a FREEFORM palette, a `get_pipeline_state {"component": null}` call is not
    decoded (`strict_sent=None`, `wire_conformant=None`) and reaches S with the `null`. Control: pass the full 19
    as `sent_tool_names` and it goes red.
  - Planner stamping (characterization of what this task builds; moved here from T8): on `OPENAI_STRICT` the
    `policy=None` planner list is 19 `strict: true` plus the terminal `strict: false`; on `NONE` no tool carries the
    key; the FREEFORM and TUTORIAL_PROFILE palettes on `NONE` equal the base bytes of §2 (checked by
    `tool_bytes.py`). For the terminal, `stamp_planner_terminal(d, NONE) == d` (type-sensitive) for the terminal built with the
    default contract and with one non-default `terminal_contract` taken from an existing planner test, and on
    `OPENAI_STRICT` the only difference is `function.strict = False`. So every terminal contract production passes
    keeps today's bytes on `NONE`.
  - Escape hatch: a run where `tool_contract_dialect=OPENAI_STRICT` and `escape_hatch_tool_contract_dialect=NONE`.
    The hatch call's recorded `tools` carry **no** `strict` key, and its `ComposerLLMCall.tool_contract_dialect` is
    `none`, while the ordinary call's is `openai_strict`. The reverse assignment is pinned too.
  - The hatch call uses the escape-hatch api_base (pin that the hoist kept the endpoint selection). Control: select
    the endpoint by the planner condition and it goes red.
  - The stamp assertion: pass an unstamped list with `OPENAI_STRICT` and `call_model` raises `AuditIntegrityError`
    before any provider call. The existing manifest pins (`test_pipeline_planner.py:1743-1748`, `:1758`) stay green
    unchanged.
- **Gates:** as T6, plus `tests/unit/web/composer/test_pipeline_planner.py`, `test_capability_skill_identity.py`,
  `tests/integration/web/composer/test_freeform_pipeline_planner.py` and `tests/integration/web/composer/parity/`.
  - The planner request byte budget: measure `request_size` (`:3896`) for a default freeform request on
    `OPENAI_STRICT` against `budget_policy.max_request_bytes`, and record the headroom here.
    **Measured at T7 (M, `$L/t7-request-bytes.log`):** the first request of an information-aware FREEFORM run
    (16 tools, `build_system_prompt(None)`) is 198,905 B on `NONE` and 199,192 B on `OPENAI_STRICT` (+287 B),
    against the default `composer_planner_max_request_bytes` of 2,097,152 B: 1,897,960 B of headroom on
    `OPENAI_STRICT`.
  - `tool_bytes.py` still shows all four base hashes (it now passes `ToolContractDialect.NONE` to the planner
    builder).
- **Commit:** `feat(planner): per-route tool-contract dialect, hoisted endpoint choice, discovery wire decode`.

### T8 — The flip: resolve per route, stamp, and probe what is sent (the first wire change)

This is one commit, because probe/production parity requires the service and the probe to move together.

- **Add** to `strict_transport.py` (D20): `resolve_composer_tool_contract(settings, *, env: Mapping[str, str]) -> ComposerToolContract`.
  It calls `resolve_strict_transport` for the planner route (`composer_model`, `composer_endpoint_base_url`) and the
  hatch route (`composer_advisor_model`, `composer_advisor_endpoint_base_url`) with `composer_strict_tools`, and
  returns an owned frozen dataclass: the setting, and per route the `StrictTransportResolution` (transport and D24
  diagnostic) and the dialect. `env` has no default.
- **Modify** `ComposerServiceImpl.__init__` (next to `service.py:2511-2515`):
  - `self._tool_contract = resolve_composer_tool_contract(settings, env=os.environ)`;
  - `self._planner_dialect` and `self._hatch_dialect` come from it, replacing T6/T7's `NONE`.
  - Expose a read-only `tool_contract_summary` property returning an owned frozen dataclass (the contract plus
    `loop_strict_true`, `loop_tool_count`). It is **not** published (D14); it feeds the structured log below and the
    tests.
  - Emit one structured log event through the module `slog` (`service.py:289`):
    `slog.info("composer_tool_contract_resolved", setting=…, planner_transport=…, planner_dialect=…,
    planner_diagnostic=…, hatch_transport=…, hatch_dialect=…, hatch_diagnostic=…, loop_strict_tool_count=…,
    loop_tool_count=…)`. Closed values and counts only: no URL and no env value (D24). This is where
    per-route transport and the effective strict count go under ruling 2. (Policy note for John: the
    logging-telemetry skill routes config lifecycle events to telemetry; the web boot path's precedent is `slog`
    beside the OTel boot counter, `app.py:742-841`, and the lead's ruling names structured logs, so S1 follows that
    precedent. The per-call effective count is also audited on every `ComposerLLMCall`, T5.)
- **Modify** `boot_probe.build_composer_probe_requests(settings, *, env: Mapping[str, str] = os.environ)` (`:135`,
  D20). It calls `resolve_composer_tool_contract(settings, env=env)`. `app.py:750` passes `os.environ` explicitly.
  The 6 existing test calls (`test_boot_probe.py` ×4, `test_boot_probe_production_parity.py:198`,
  `test_composer_against_gateway.py:538`) are left as they are; the autouse env scrub below keeps them hermetic.
  - `loop_tools` sends `composer_loop_tool_definitions(planner dialect)`.
  - `planner_tools` sends `planner_tool_definitions(dialect=planner dialect)`.
  - **New surface `hatch_terminal`** (D9), added only when the hatch resolves non-`NONE`:
    - role `planner`, model `composer_advisor_model`, advisor endpoint and key;
    - `tools=[planner_terminal_tool_definition(dialect=hatch dialect)]`;
    - `build_planner_request_kwargs` at candidate effort (as `call_model` on a hatch turn), then
      `kwargs["max_tokens"] = LOOP_PROBE_MAX_TOKENS` (16), exactly as `loop_tools` does (`boot_probe.py:157`).
      **Ruled (lead, provisional; John may overrule), §7.3 item 1:** a rejection check only; the token cap is the one
      deliberate difference from a hatch turn's request;
    - no cache markers, because D8 means no Anthropic-family route is non-`NONE`.
  - Order: `loop_tools`, `planner_tools`, `hatch_terminal`, `advisor`. Extend `ComposerProbeSurface` (`:45`) with
    `"hatch_terminal"`.
- **Modify** `app.py:742-841`: no constant changes. The existing `role == "planner"` rule gives `hatch_terminal` the
  5 s cap, and the advisor gets the remainder (at least 30 s on a 4-surface boot). The telemetry `probed_surface`
  gains the new value.
  - **Ruled (lead, provisional; John may overrule), §7.3 item 8, option (b):** a `hatch_terminal` rejection is
    non-fatal. The `except ComposerBootConfigError` arm (`:828-830`) gains a surface-specific branch: when
    `probe_request.surface == "hatch_terminal"`, set `probe_status = "rejected"`, emit
    `slog.warning("composer_boot_probe_rejected_nonfatal", model=…, probed_role=…, probed_surface=…,
    tool_count=…, strict_true_count=…, strict_false_count=…, strict_key_omitted=…, action="booting; the escape-hatch terminal was rejected at boot; hatch turns will fail until the hatch route or composer_strict_tools is changed")`,
    following the `composer_boot_probe_transient_failure` pattern (owned facts only; never the exception text or
    cause chain), and `continue`. Every other surface re-raises exactly as in S0, so planner-route and advisor 400s
    stay fatal. The `finally` block still records `probe_status = "rejected"` on the OTel counter.
- **Test housekeeping:**
  - replace T6/T7's `service._planner_dialect = …` assignments with settings that resolve to the dialect: an
    OpenRouter planner with no custom base (`openrouter/deepseek/deepseek-v4.1-flash`, row 4) → `OPENAI_STRICT`;
    the default `gpt-5.5` → `NONE` under ruling 7 (C17); `composer_strict_tools="off"` on the OpenRouter planner →
    `NONE`. Where a test needs `ENFORCING` specifically, it sets `composer_strict_tools="forward_to_endpoint"` on
    `gpt-5.5`;
  - add `composer_strict_tools` to every structural settings double from T4's inventory;
  - **env hermeticity.** From this commit every compose-loop test's wire depends on the resolver, which
    reads `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENROUTER_API_BASE` and `AZURE_API_VERSION`, and pytest loads
    `.env` (§2). Add an autouse fixture that `monkeypatch.delenv`s those four (with `raising=False`) to
    `tests/unit/conftest.py` and `tests/integration/web/conftest.py`. Not the root conftest: the live provider
    tests under `tests/integration/plugins/` read Azure settings from the environment. Control (rewritten for
    ruling 7, because `OPENAI_BASE_URL` no longer changes the default `gpt-5.5` route's dialect under `preferred`):
    on the OpenRouter planner, a test with `OPENROUTER_API_BASE=https://proxy.example/v1` set through
    `monkeypatch.setenv` asserts the service resolves `NONE` (row 5), and one without it asserts `OPENAI_STRICT`
    (row 4); and on `gpt-5.5` under `forward_to_endpoint`, `OPENAI_BASE_URL=https://gw.example/v1` gives transport
    `FORWARDING` (row 8) and its absence gives `ENFORCING` (row 7), asserted on `tool_contract_summary`'s transport.
  - **Malformed env regression (D24, Codex finding 1).** Construct `ComposerServiceImpl` with
    `composer_boot_probe_enabled=False`, the OpenRouter planner and advisor, and
    `monkeypatch.setenv("OPENROUTER_API_BASE", "http://[")`: construction succeeds, both routes resolve `NONE` with
    diagnostic `unparseable_openrouter_api_base`, the loop list carries no `strict` key, and the
    `composer_tool_contract_resolved` event carries the diagnostic but not the value (captured with
    `structlog.testing.capture_logs`). Do the same through `build_composer_probe_requests(settings, env=…)`: no
    `hatch_terminal` surface and no raise. Control: remove the resolver's `ValueError` catch and construction
    raises `ValueError`.
- **Tests: deliberate pin moves (each one listed in the commit message):**
  - `test_boot_probe.py` (rewritten for ruling 7, C17; R at `85ebf2739`):
    - `:76`, `:79` (0, 0, 42) and `:93` (0, 0, 20) **stay unchanged**: their planner is `gpt-5.5` with no base, which
      is `NONE` under `preferred`. The builders they compare against take `ToolContractDialect.NONE` from T2/T7.
    - `:96-106` (mutation control) stays unchanged on the `gpt-5.5` `NONE` route, where (1, 1, 40) holds. Add a
      second control on a stamped base: an OpenRouter planner (`openrouter/deepseek/deepseek-v4.1-flash`, no base),
      flip one `true` to `false` → (31, 11, 0).
    - Add stamped counterparts on that OpenRouter planner: `loop_tools` equals
      `composer_loop_tool_definitions(ToolContractDialect.OPENAI_STRICT)` with counts (32, 10, 0), and `planner_tools`
      equals `planner_tool_definitions(dialect=OPENAI_STRICT)` with counts (19, 1, 0).
    - `:292-316`: the parametrisation gains `("hatch_terminal", 1)`. The hard-coded
      `strict_true_count=0, strict_false_count=0, strict_key_omitted={tool_count}` at `:316` becomes per-surface
      expected counts: `(0, 0, 42)`, `(0, 0, len(planner list))`, `(0, 1, 0)` for `hatch_terminal` (the default
      OpenRouter advisor is `FORWARDING`), and `(0, 0, 0)` for `advisor`. So the new surface's
      `ComposerBootConfigError` text is tested.
    - `:61-70` keeps its 3 surfaces because its advisor is `anthropic/` (row 2). `:212` keeps its 3-tuple unpack
      because its advisor is `gpt-5.5` + a custom advisor base (row 8). `:222` iterates every request with the
      default OpenRouter advisor, so it now also covers `hatch_terminal`; its assertion (no `api_base`/`api_key`
      when both endpoints are unset) holds for it unchanged. Re-check each reason when running.
    - Add `test_requests_include_hatch_terminal_on_a_forwarding_hatch`, using the fixture's default OpenRouter
      advisor: surfaces `[loop_tools, planner_tools, hatch_terminal, advisor]`, the hatch tool list is exactly the
      stamped terminal with `strict: false`, `max_tokens == LOOP_PROBE_MAX_TOKENS == 16` (ruling 1), the reasoning
      effort is the candidate effort, and the model, endpoint and key are the advisor's (set the advisor pair to
      `https://openrouter.ai/api/v1` plus a key, `config.py:1308-1322`, which stays row 4 `FORWARDING` while
      differing from the unset planner endpoint, so the endpoint assertion can fail; a non-OpenRouter advisor base
      would be row 5 and drop the surface). Control: build it with the planner
      token cap and the `max_tokens` assertion goes red.
    - Add `test_hatch_terminal_rejection_is_nonfatal` (in `test_app.py`, beside the lifespan budget tests): a
      scripted 400 on `hatch_terminal` → the app boots, a `composer_boot_probe_rejected_nonfatal` event is captured
      and the counter records `rejected`; the same 400 on `planner_tools`, on `loop_tools` and on `advisor` →
      `ComposerBootConfigError` propagates as in S0 (the surface-specific control). Control: drop the surface check
      so every 400 is non-fatal, and the `planner_tools` case goes red.
  - `test_tool_schema_contract.py:36-57` and `test_provider_cache_markers.py:242-270`: they are true only for
    today's bytes, so they already pass `NONE` from T2. Add an `openai_strict` counterpart in a new test, not by
    editing their expectations.
  - `test_app.py`: the 3-surface pins (`:1968`, `:2017`, `:2077`, `:2165`, `:2169`, `:2202`) stay unchanged because
    their hatch resolves `NONE`: most use the default `anthropic/` advisor (row 2), and the one near `:1995` uses
    advisor `gpt-5.5` with a custom advisor base (row 8). Re-check each one's reason when running it. Add a 4-surface budget test with an OpenRouter
    advisor. Model the clock exactly as the existing test at `:2061-2083` does; that test does **not** advance the
    clock by 5 s per probe, and gives the advisor `44.0 < t <= 45.0` after two planner probes. Assert:
    - the surface order `[loop_tools, planner_tools, hatch_terminal, advisor]`;
    - each of the three planner-role probes is capped at `min(remaining, 5.0)`;
    - the advisor's timeout is the `remaining` value after them.
    Do not hard-code 30 s. The worst case of at least 30 s (45 − 3 × 5) is the D9 claim; the test pins the rule.
    Control: order the hatch after the advisor and it goes red.
  - The `test_boot_probe_production_parity.py` pins stay green (custom endpoint → `NONE`). The existing planner
    parity test excludes tools (`:248` compares with `"tools"` removed; `:252` checks only that names are a subset),
    so **add** `test_probe_and_production_send_the_same_strict_flags_on_a_forwarding_route`: an
    `openrouter/deepseek/…` planner and an `openrouter/z-ai/glm-5.3` advisor, with a monkeypatched
    `litellm.acompletion` capturing both sides. It asserts three things:
    - the per-tool `strict` flags of `loop_tools` equal those of a real `compose()` turn;
    - the name → `strict` map of `planner_tools` agrees with a production planner request on every name the two
      share (the palettes differ, so compare on the intersection, and assert the intersection is not empty);
    - the terminal's flag in `hatch_terminal` equals the one on a production hatch turn (`tools_override` path,
      `pipeline_planner.py:4260`).
    - Also assert that `resolve_composer_tool_contract` gives the same contract from the probe's call and from the
      service (D20).
    - Controls: stamp the probe's loop list with `NONE` and it goes red; stamp the probe's planner list with `NONE`
      and it goes red. (Both routes here resolve to `OPENAI_STRICT`, so a hatch-dialect substitution cannot turn
      this test red, Codex finding 3; the hatch routing control is the next test.)
  - **Add** `test_hatch_probe_and_hatch_turn_follow_the_hatch_route_not_the_planner_route` (Codex finding 3), in
    the same file, with asymmetric resolved routes in **both** directions. Each `NONE` side comes from the routing
    table (row 5, via a custom base+key pair, `config.py:1308-1322`), not from D8's Anthropic short-circuit, so the
    resolver itself is exercised:
    - (i) planner `openrouter/deepseek/…` with no base (row 4, `FORWARDING`) + advisor `openrouter/z-ai/glm-5.3`
      with advisor base `https://proxy.example/v1` and key (row 5, `NONE`): the probe has **no** `hatch_terminal`
      surface; `loop_tools`/`planner_tools` are stamped; a production hatch turn goes to `https://proxy.example/v1`
      and its transmitted `tools` carry no `strict` key, and its `ComposerLLMCall.tool_contract_dialect` is `none`;
    - (ii) planner `openrouter/deepseek/…` with base `https://proxy.example/v1` and key (row 5, `NONE`) + advisor
      `openrouter/z-ai/glm-5.3` with no base (row 4, `FORWARDING`): the probe **has** `hatch_terminal`, sent to the
      advisor's effective endpoint (no `api_base`, i.e. OpenRouter's default) and not to the planner's proxy, with the
      terminal transmitted as `strict: false`; `loop_tools`/`planner_tools` carry no `strict` key; a production hatch
      turn transmits the terminal with `strict: false` to the same endpoint.
    - Assert on the captured `litellm.acompletion` kwargs (the transmitted request), not on the builders' return.
    - Control: substitute the planner's dialect for the hatch dialect (in `build_composer_probe_requests`'s
      inclusion check and in `PlannerModelConfig.escape_hatch_tool_contract_dialect` at the service construction
      sites) and **both** directions go red: (i) gains a `hatch_terminal` surface and a stamped hatch turn, (ii)
      loses them. Record both logs.
- **Tests (new):**
  - `tests/unit/web/composer/test_compose_loop_llm_audit.py`-style: a compose turn on the OpenRouter planner
    (`openrouter/deepseek/deepseek-v4.1-flash`, no base, default `preferred`) records
    `tool_contract_dialect="openai_strict"`, `strict_tool_count=32`. The same planner with
    `composer_strict_tools="off"` records `none`/`0` and sends tools with no `strict` key. A default-settings turn
    (`gpt-5.5`, `preferred`) also records `none`/`0` and sends no `strict` key (ruling 7: the default route keeps
    today's bytes); with `forward_to_endpoint` the same `gpt-5.5` turn records `openai_strict`/32.
  - No provider call is added to a compose transition (the composer invariant, checked behaviourally). One scripted
    freeform compose turn and one scripted tutorial-entry turn, each run on the OpenRouter planner under `preferred`
    (`OPENAI_STRICT`) and with `composer_strict_tools="off"` (`NONE`), make the same number of captured
    `litellm.acompletion` calls on both dialects. The existing call-count pins in `tests/unit/web/composer` (for example in
    `test_compose_loop_carriers.py`, `test_provider_telemetry.py`) must pass unchanged in the gate run below. Control:
    add a second provider call on the strict path and the test goes red.
- **Gates:**
  - the whole of `tests/unit/web/composer`, `tests/unit/web/test_app.py`, `tests/unit/web/test_composer_bedrock.py`
    (the Bedrock `NONE` pin `:94-95` must stay green unchanged; if it goes red, the resolver misclassified Bedrock)
    and `tests/integration/web/composer/` (the gateway file must pass unchanged: 9 tests, bare `gpt-5.5` + base →
    `NONE`);
  - re-run `test_compose_loop_envelope.py` and record the new byte figure against 216,295 (do not raise the 300,000
    ceiling);
  - G-decl, G-wire, G-skill, G-tier (list any stale signed fingerprints in `service.py`/`boot_probe.py`), G-contracts,
    plus the every-task set.
  - Update the boot-probe paragraph in `docs/reference/environment-variables.md` (`:268-282`), and add the
    "(with T8)" sentence from T4 about `enforcing` first boots under `forward_to_endpoint` and the non-fatal
    `hatch_terminal` rejection.
- **Commit:** `feat(composer): send strict:true on the 32 mechanical tools on forwarding routes; probe the stamped lists`.

### T9 — S-gate repair signal (closed `validation_errors`, compose loop only)

Scope (D19): the violations are built in the shared S gate and carried on the exception everywhere, but only the
compose loop renders them. MCP text (`composer_mcp/server.py:781-801`) and planner discovery's
`_ArgumentErrorResponse` (`provider_discovery_response.py:186-202`) are unchanged, so §5 "MCP unchanged" holds.
Ruled (lead, provisional; John may overrule), §7.3 item 9: it is not extended; the repair signal stays compose-loop
only.

Deploy order. **Ruled (lead, provisional; John may overrule), §7.3 item 10:** T9 ships with S1. T9 changes
planner-visible tool results on every S-gated tool, and `off` does not revert it. If S0 and S1 deploy together, the
S0 baseline window (S0 acceptance 5) is taken with `composer_strict_tools=off`, and the T9 repair signal is a stated
confound of that baseline (it is live in both windows, so it cannot be separated from S0's own effect there, and
the `off` → `preferred` comparison isolates the strict flip only). The deploy time and the setting change time are
recorded so later readings can be split at those boundaries (§6 step 6).

- **Modify** `src/elspeth/web/composer/protocol.py` `ToolArgumentError`: add a keyword-only
  `schema_violations: tuple[SchemaViolation, ...] = ()`, where `SchemaViolation(loc: tuple[str, ...], code: SchemaViolationCode)`
  is owned and frozen, and `SchemaViolationCode` is a closed StrEnum whose values are a subset of the pydantic
  canonicaliser's (`audit.py:1316-1324`): `missing`, `unexpected`, `invalid_choice`, `invalid_type`,
  `out_of_bounds`, `invalid`. (The canonicaliser also has `invalid_value`, which the S gate has no source for, and
  `truncated`, handled below.) The class stays `@final` and frozen.
- **Modify** `tools/_dispatch.py` `_schema_tool_argument_error` (`:538`): build violations from **all**
  `_schema_errors` (not only the reported one). Truncation follows the canonicaliser (`audit.py:1302-1309`): at most
  8 violations are kept; when there are more than 8, the payload carries the single entry
  `{"loc": [], "msg": "Validation produced more than 8 errors", "type": "truncated"}` instead, exactly as the
  pydantic path does.
  - The loc is the `absolute_path` walked through the tool's own schema. A string segment is kept only if it is a
    declared property name at that schema node; an integer becomes `index`; anything else becomes `field` at
    position 0 and `item` after. Depth is capped at 4.
  - `required` gives the parent loc plus the missing declared name, with code `missing`.
  - `additionalProperties` gives the parent loc, code `unexpected`, and **never the key** (C10).
  - The validator keyword maps to the code: `enum`/`const` → `invalid_choice`; `type` → `invalid_type`;
    `minimum`/`maximum`/`exclusive*`/`multipleOf`/`minLength`/`maxLength`/`minItems`/`maxItems` → `out_of_bounds`;
    anything else → `invalid`.
  - `require_arguments_conform_to_schema` (MCP session tools) walks its own caller-owned schema the same way, so the
    exception carries violations there too; MCP does not render them.
- **Modify** `tool_error_payloads.arg_error_payload` (`:33-38`): when `canonicalize_pydantic_cause` gives `None` and
  `exc.schema_violations` is non-empty, emit `validation_errors` as `[{"loc": [...], "msg": <fixed text per code>,
  "type": code}]`, using the same fixed messages as `audit.py`'s `type_messages`. Its callers are the compose loop
  only (`tool_batch.py:2419`, `:2474`; `service.py:8005`, `:8098`, R). Redaction already persists only
  `validation_error_count` (`redaction.py:406-408`), so no new stored content appears.
- **Env doc:** add the "(with T9)" sentence from T4: `off` does not revert `validation_errors`.
- **Tests (RED):** `tests/unit/web/composer/test_schema_gate_repair_signal.py`, all through the compose loop (both
  `arg_error_payload` callers: an ordinary tool through `tool_batch`, and a carve-out through `service.py:8005` or
  `:8098`).
  - `list_models {"limit": 0}` gives `validation_errors == [{"loc": ["limit"], "type": "out_of_bounds", …}]`. The
    RED premise is measured: today the payload is `{'error': "Tool 'list_models' failed: … got invalid_schema"}`
    with no `validation_errors` (M, reality critic).
  - An extra key gives `loc: []`, `type: "unexpected"`, and the payload text does not contain the key. Control:
    echoing the key turns the negative assertion red.
  - A missing required field on a nested object gives the declared name.
  - A non-identifier or undeclared path segment becomes `field`/`item`.
  - More than 8 errors give the single `truncated` entry.
  - Unchanged surfaces, pinned: an MCP session tool's error text and a planner discovery `argument_error` body are
    byte-equal to today's for the same bad input. Control: render violations into the MCP text and it goes red.
- **Pin moves:** none are predicted. The four pins the first draft listed were checked and do not move
  (`test_call_tool_audit.py:258` asserts MCP text; `test_tool_argument_type_guidance_gate.py:176` asserts
  `exc.expected`; `test_pipeline_commit_operation_authority.py:300-327` raises a hand-built error with no violations
  and reads the planner factory's payload; `test_compose_loop_interpretation_review_dispatch.py` asserts class and
  category only). Derive the real list by running `tests/unit/web/composer` and `tests/unit/composer_mcp` after the
  change, and list every moved pin in the commit message. Each allowed move adds the `validation_errors` key to a
  compose-loop payload and nothing else.
- **Gates:** `test_adequacy_guard.py` and the redaction tests (`test_tool_redaction_policy.py`,
  `tests/unit/web/sessions/test_tool_invocation_redaction.py`), G-census-S0 (the error-code registry stays unchanged,
  because these are not error codes), G-cec (no bare raises in `_dispatch.py`, which is exempt anyway), G-contracts,
  the every-task set, and the whole of `tests/unit/web/composer` and `tests/unit/composer_mcp`.
- **Commit:** `feat(composer): S-gate rejections carry closed validation_errors (loc + code, never the offending key)`.

### T10 — `/api/system/status` `composer_tool_contract`

**Ruled (lead, provisional; John may overrule), §7.3 item 2 (D14, Codex finding 2):** the public key carries only
closed, route-independent values. The first draft's per-route `transport`/`dialect`, effective `loop_strict_tools`
and per-surface `boot` outcomes are **not** published, and the lifespan `app.state.composer_boot_probe_outcomes`
store is **not** added.

- **Add** to `wire_projection.py` two pure public accessors, so `app.py` does not import the private store:
  `strict_capable_tool_count() -> int`, the number of `strict_capable` tools in
  `_WIRE_TOOL_DEFS[ToolContractDialect.OPENAI_STRICT]` (32 at `85ebf2739`), and `loop_tool_count() -> int`
  (`len(_WIRE_TOOL_DEFS[ToolContractDialect.NONE])`, 42). They read the tool set, not a route, a setting or a sent
  list.
- **Modify** `app.py`:
  - `system_status` (`:2269`) adds:
    ```json
    "composer_tool_contract": {"setting": "preferred", "strict_capable_tool_count": 32, "tool_count": 42}
    ```
    - `setting` is `settings.composer_strict_tools` (closed `Literal`); `strict_capable_tool_count` comes from the
      accessor above; `tool_count` from `loop_tool_count()`. None of them depends on
      the model, the endpoint, the env or the boot outcome, so the key is published whether or not the composer is
      available (availability is already published as `composer_available`). The name says "capable", so a reader
      cannot take it for what a route sends.
  - The lifespan probe loop's `finally` block (`:837-841`), beside the existing OTel counter, emits
    `slog.info("composer_boot_probe_outcome", probed_surface=…, probed_role=…, probe_status=…, tool_count=…,
    strict_true_count=…, strict_false_count=…, strict_key_omitted=…)` (owned facts only). With T8's
    `composer_tool_contract_resolved` event, that is where per-route transport, effective counts and per-surface
    boot outcomes go (policy note in T8).
- **Tests (RED):** `tests/unit/web/test_system_status_tool_contract.py`:
  - default settings → `{"setting": "preferred", "strict_capable_tool_count": 32, "tool_count": 42}`;
  - `off` → the same counts, `setting: "off"`;
  - **route independence (Codex finding 2):** an OpenRouter planner (`openrouter/deepseek/…`, no base → row 4,
    effective 32) and the same config with `composer_endpoint_base_url=https://proxy.example/v1` plus key (row 5,
    effective 0) give **byte-identical** `composer_tool_contract` values. The test first asserts, through
    `composer_service.tool_contract_summary`, that the two configs' effective loop strict counts do differ (32 vs 0),
    so the identity is not vacuous. The same pin for the advisor route (row 4 vs row 5 on the hatch).
  - probes enabled or disabled, and a scripted `hatch_terminal` rejection, give the same `composer_tool_contract`.
  - the boot-outcome events: with probes enabled on the OpenRouter pair, one `composer_boot_probe_outcome` event per
    sent surface is captured (`structlog.testing.capture_logs`), and none carries a URL or key.
  - Control: publish the effective count (`tool_contract_summary.loop_strict_true`) in place of the accessor and the
    route-independence pin goes red.
  - No exact-key-set pin on the body exists (measured, `understand-transport.md` §6), so no existing test moves.
- **Gates:** `tests/unit/web/test_app.py`, the ACA/ECS acceptance readers (`azure_container_apps_single_revision.py:103-107`
  ignores extra keys; `_aws_ecs_acceptance/capture.py:559-600` reads named keys): run their unit tests, plus the
  every-task set.
- **Commit:** `feat(web): /api/system/status reports the strict-tools setting and strict-capable tool count; boot outcomes to structured logs`.

### T11 — Wire fidelity matrix and the gateway known-negative row

- **Create** `tests/unit/web/composer/test_wire_fidelity_matrix.py`. It promotes `$L/wire_recorder.py` and
  `$L/respx_hosted_probe.py`. No network beyond `127.0.0.1`. Its rows are **characterization** (green on arrival);
  the known-negative rows are what show the recorder reads the transformed wire body.
  - **Precondition:** assert `get_model_cost_map_source_info()["source"] == "local"`
    (`litellm.litellm_core_utils.get_model_cost_map`) before any row runs (C18, C24).
    - `core/litellm_policy.configure_litellm_pricing()` only does
      `os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")` (`litellm_policy.py:6-8`). It works only if it
      runs **before** LiteLLM is first imported in the process, and it cannot re-apply after a remote load.
    - Today the precondition holds only because `tests/unit/web/conftest.py` happens to load ELSPETH pricing
      before LiteLLM (C24). So, in this task, call `configure_litellm_pricing()` at the top of the root
      `tests/conftest.py`, before anything imports `litellm`, with a comment giving the reason. This is a
      deliberate, whole-suite change: it only sets a default, and the tests that exercise the variable
      (`tests/unit/core/test_litellm_policy.py`, `tests/unit/plugins/llm/test_model_catalog.py`,
      `tests/unit/plugins/llm/test_bundled_terra_pricing.py`) pop or override it in their own subprocess env; run
      those three files to confirm. Never weaken the assertion.
  - The loopback recorder is `ThreadingHTTPServer(("127.0.0.1", 0), …)`, the precedent at
    `tests/unit/test_ci_workflow_xdist.py:890`, serving canned responses per route shape.
  - Hosted rows use `respx`, which is already a dev dependency (`pyproject.toml:121`).
  - Requests are built through `build_composer_loop_request_kwargs` / `build_planner_request_kwargs`, with the list
    for the route's **resolved** dialect under the row's **named** setting. The design lane's
    `tool_choice`/`parallel_tool_calls`/`require_parameters` shape is not used. Set `num_retries=0`, use fake keys,
    and give Bedrock fake `aws_*` env through `monkeypatch`.
  - **Settings per row (Codex finding 4).** A loopback base is a custom endpoint: `openrouter/…` + loopback is row 5
    and `openai/…` or bare `gpt-5.5` + loopback is row 8, both `NONE` under `preferred`. Under ruling 7, hosted OpenAI
    (row 7) and Azure (row 9) are `NONE` under `preferred` too. So every row below that measures adapter forwarding
    of a stamped list resolves with `setting="forward_to_endpoint"` **explicitly**, and asserts first that the
    resolver gives the expected non-`NONE` transport for that setting. Each such family also has a separate
    **`preferred` control row** on the same route: the resolver gives `NONE`, the `NONE` list is built, and the
    transmitted tools carry no `strict` key (today's bytes).
  - Rows. Each asserts the transmitted `function.strict` (or its absence) and the schema caveat:
    - `openrouter/deepseek/…` (loopback base), `forward_to_endpoint` (row 5 → `FORWARDING`): `strict` true on 32 and
      false on 10, verbatim; an ECMA-only `pattern` in a synthetic tool is kept. `preferred` control: no `strict` key;
    - `openai/…` + an openrouter-host base (loopback cannot present that host, so drive `openai/` + loopback under
      `forward_to_endpoint` (row 8 → `FORWARDING`) and assert forwarding, and separately assert the resolver's
      `FORWARDING` for the real host under `preferred`, row 6). `preferred` control on loopback: no `strict` key;
    - bare `gpt-5.5` + loopback under `preferred`: no `strict` key on any tool (today's bytes). Under
      `forward_to_endpoint`: forwarded verbatim;
    - hosted `gpt-4.1` chat (respx `api.openai.com/v1/chat/completions`), `forward_to_endpoint` (row 7 →
      `ENFORCING`): `strict` true. `preferred` control: no `strict` key (ruling 7);
    - hosted `gpt-5.5` with tools (respx `/v1/responses`), `forward_to_endpoint`: `strict` true. `preferred` control:
      no `strict` key;
    - known-negative: with the key omitted (the `off` list), the bridge sends `strict: null`;
    - `azure/gpt-4.1` chat and `azure/gpt-5.5` bridge (loopback `api_base`), `forward_to_endpoint` with env
      `AZURE_API_VERSION` unset (row 9 → `ENFORCING`): `strict` forwarded. `preferred` control: no `strict` key;
    - known-negative: a synthetic root `oneOf` is flattened on Azure chat;
    - known-negative: `anthropic/…` (loopback) with a **forced** stamped list: `strict` absent on the wire. This
      is why D8 resolves Anthropic to `NONE`;
    - known-negative: `bedrock/anthropic.…` converse (loopback endpoint): a synthetic `enum: [..., null]` loses
      its `null`;
    - known-negative for the openai-compatible chat family (bare `gpt-5.5` or `openai/…` + loopback): a synthetic
      ECMA-only `pattern` (`^\p{L}+$`) is **removed** on the wire, while the OpenRouter row keeps it verbatim. The
      design-lane recorder measured both (`wire_recorder.summary.txt`: `patterns={'x': None, …}` on the custom-base
      rows, `{'x': '^\\p{L}+$', …}` on OpenRouter). The pair shows the recorder reads what LiteLLM put on the wire,
      not the kwargs;
    - resolver drift is pinned by T4's literal provider column, not repeated here (comparing the resolver with
      `get_llm_provider` would be a tautology).
  - Controls: every adapter family the matrix claims to observe (OpenRouter, openai-compatible chat, hosted OpenAI
    chat and bridge, Azure chat and bridge, Anthropic, Bedrock) has at least one known-negative row, or a row whose
    result differs from another family's on the same input. Where a family has neither, the row says so in its
    docstring. Each known-negative is also run once with its transformation input removed (the forced
    stamped list on Anthropic, the `null` in the Bedrock enum, the root `oneOf` on Azure) and must then show the
    untransformed form; keep the logs in `$L`. Flipping an expected value is not a control.
  - No `getattr`/`hasattr` (G-masq), no `rglob` (G-walker), and specced mocks only (G-mock).
- **Extend** `tests/integration/web/composer/test_composer_against_gateway.py` **by appending new test functions
  only**. The 9 existing tests stay byte-unchanged, and the diff must show only added lines. The new tests reuse
  its `gateway_base_url` fixture:
  - a request whose tools carry `function.strict: true` → gateway 400;
  - `strict: false` → 400;
  - the list `composer_loop_tool_definitions(dialect_for(resolve_strict_transport(model="gpt-5.5", api_base=<gateway>, setting="preferred", env={}).transport))`
    → accepted, and no tool carries `strict`.
- **Docs.** `CONTRIBUTING.md` § Whole-tree gates gets one bullet: the fidelity matrix pins LiteLLM adapter behaviour
  for the strict wire, a LiteLLM bump that turns it red is fixed by re-measuring and updating the rows with evidence,
  never by loosening them, and it depends on `configure_litellm_pricing()` running in the root `tests/conftest.py`
  before LiteLLM is imported (a bare `import elspeth` does not load it). `docs/agents/recent-code-hints.md` gets a
  dated entry.
  Both go in the same commit, as `CONTRIBUTING.md:826-828` requires for a standing convention. Run `tests/unit/docs`.
- **Gates:** the two files `-n 0`, then `tests/unit/web/composer` at the default worker count (port 0 is
  xdist-safe), the three cost-map files above, the every-task set, `tests/unit/docs`. The root-conftest change has
  whole-suite reach, so the §6 full-suite gate is its real check.
- **Commit:** `test(composer): wire fidelity matrix for strict across LiteLLM routes, incl. gateway 400 known-negative`.

### T12 — Invariant proofs, master-plan record, Appendix A

- **NONE-route byte identity** (§5 below, one-shot branch evidence): run `$L/tool_bytes.py` (passing
  `ToolContractDialect.NONE`) on the branch tip, and the original zero-argument script, extended with the two
  palettes, against `$L/base-export` (with `PYTHONPATH` pointed at the export's `src`). Both must print
  `6c7f60dd…`/63,872, `7e958245…`/29,264, `709b4176…`/25,424 (FREEFORM) and `50fdbefd…`/23,960
  (TUTORIAL_PROFILE). Keep the logs.
- **Durable pin:** the relational `none` identity test added in T2 (`wire_tool_definitions(NONE)` equals S plus the
  envelope, with type-sensitive `==`, exact key sets and no `strict` key). It is not an absolute SHA pin, so a
  legitimate later S edit does not move it; the SHA comparison above is evidence for this branch only.
- **Master plan** (`docs/plans/2026-09-23-composer-strict-tool-contracts.md`):
  - add "S1 as implemented: corrections and decisions", listing C1-C24, D1-D24 and the §7.3 lead rulings briefly, with the commit ids and
    the measured figures (the byte delta, the envelope bytes, the planner request headroom);
  - mark S1 status;
  - fix §5.3's "read from the invocation" wording.
- **Appendix A:** the `calls` CTE gains `json_extract(tc.value, '$.strict_sent') AS strict_sent` and
  `json_extract(tc.value, '$.wire_conformant') AS wire_conformant`, grouped into the output. Re-run the S0 fixture
  census (the `$OLD/s0-fix/appendix_a_fixture.py` pattern) against a fixture DB written by `persist_compose_turn` on an
  `OPENAI_STRICT` context: one conformant row, one non-conformant row, one option-tool row (`strict_sent` false),
  and the two `other_failure` controls; plus one row written on a `NONE` context, which must group with
  `strict_sent` `NULL` (D16), apart from the strict-route option-tool row. Negative control: an entry without the
  keys groups as `NULL` too, so the census groups by `wire_conformant` as well to tell a pre-S1 row (both `NULL`)
  from a `NONE`-route row (`strict_sent` `NULL`, `wire_conformant` set). Keep the logs in `$L`.
- **CHANGELOG.** S1 is a wire change and adds an operator setting (`composer_strict_tools`), so it needs a
  CHANGELOG line. Ask John which release it belongs to; do not infer it from the branch name. Add the line under
  that release in this commit, or in a follow-up once he answers.
- **Commit:** `docs(plans): record S1 as implemented; Appendix A reads compose-loop wire facts`.

---

## 5. Byte and behaviour invariants to prove

| Invariant | Instrument | Control |
|---|---|---|
| NONE-route tool bytes are identical to the base, for the loop list, the `policy=None` planner list and the two production palettes | `tool_bytes.py` on the tip (`NONE`) and on `$L/base-export`: loop 63,872 B `6c7f60dd…`, planner 29,264 B `7e958245…`, FREEFORM 25,424 B `709b4176…`, TUTORIAL_PROFILE 23,960 B `50fdbefd…`, compact UTF-8 form (branch evidence) | 1-character mutation changes the hash (`tool_bytes_base.log`) |
| The `none` W is S (the durable form of the row above) | T2's relational test: type-sensitive `==` against `get_tool_definitions()` plus the envelope, and a `type(x) is list`/`dict` walk; `test_provider_cache_markers.py:253-261` | a thaw that leaves a tuple goes red (the SHA-256 cannot see it, §2) |
| Every tool on a NONE route carries exactly `{"type", "function": {name, description, parameters}}` | `test_composer_bedrock.py:94-95` (Bedrock route) + a new loop over `wire_tool_definitions(NONE)` in `test_wire_projection.py` | planting `strict` on one tool goes red |
| S unchanged | G-decl: `test_tool_declarations.py` passes with **no diff to the file**, and `git diff 85ebf2739 -- src/elspeth/web/composer/tools/_dispatch.py` shows only T9's error-builder lines (no definition edits) | a definition edit turns G-decl red |
| The gateway integration tests stay green unchanged | `test_composer_against_gateway.py` 9 original tests, plus a diff showing only appended functions | the new strict-row tests (400 on `strict`) |
| Decode never rejects on W failure | `test_wire_decode.py` | a rejecting decode goes red |
| No provider call is added to any compose transition | (1) Behavioural, the real check: T8's scripted freeform and tutorial-entry turns make the same number of captured provider calls on `OPENAI_STRICT` and `NONE`, and the existing call-count pins in `tests/unit/web/composer` pass unchanged. (2) Static, supporting: `rg -n -e acompletion -e "completion\(" src/elspeth/web/composer` diffed against the base export shows **0 new call sites**. The `hatch_terminal` probe adds a request, not a call site: it goes through the existing `_litellm_acompletion` at `boot_probe.py:222`. A call-site regex cannot see extra invocations of an existing site, which is why (1) exists | (1) a second provider call on the strict path turns T8's test red; (2) the same `rg` finds the existing `_litellm_acompletion` (positive hit) |
| Wire facts agree between the invocation and the P4 row | T6's parity pin over the SUCCESS, non-object, envelope, JSON-failure and unknown-tool branches | dropping the explicit facts at `tool_batch.py:948` goes red |
| Probe and production send the same stamps | T8's forwarding-route parity test: loop flags, planner flags on the shared names, and the hatch terminal flag | stamping either probe list with `NONE` goes red |
| The hatch probe and hatch turn follow the hatch route | T8's asymmetric-route test in both directions: `hatch_terminal` inclusion/omission, effective endpoint, transmitted flags | substituting the planner dialect for the hatch dialect goes red in both directions |
| The public status payload is route-independent (ruling 2) | T10: normal-OpenRouter-host and custom-endpoint configs give byte-identical `composer_tool_contract`, after asserting their effective counts differ | publishing the effective count goes red |
| A malformed env base never stops app construction (D24) | T4 resolver rows with `http://[` / `not-a-url`; T8 service construction with probes disabled | removing the `ValueError` catch raises at construction |
| Byte delta of the strict form | `wire_tool_definitions(OPENAI_STRICT)` vs `NONE`, compact form, recorded in T2 (M, `tool_bytes.py` extended): the 32 strict-capable tools are 28,497 B on `NONE` and 29,462 B on `OPENAI_STRICT` (**+965 B**: 448 B are the 32 `"strict":true` stamps, 517 B are the `anyOf`/nullable promotions, ledger text and "pass null" overrides). The whole 42-tool loop list is 63,872 B → 64,987 B (+1,115 B, including the 10 `"strict":false` stamps). This replaces the master plan's enum-null figure (28,464 → 28,814, measured on a per-tool sum without the list's commas and brackets and without stamps) | — |
| MCP unchanged | `tests/unit/composer_mcp` green, MCP's advertised list equals the base (`_build_tool_defs` output hashed on tip and base), and MCP error text is unchanged by T9 (T9's unchanged-surface pin) | rendering violations into the MCP text turns T9's pin red |

---

## 6. Full-suite gate and merge procedure

1. Check the capacity of the host: no other suite running, and acceptable load. Then run one broad suite:
   `cd $W && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest`.
   - Read the `stages :` launch line; the default is only ruff and pytest.
   - Wait on the printed `.done` path (never a `pgrep -f` loop).
   - Read `summary.txt`: every stage's exit code, and `frozen=yes`. `frozen=NO` means the run is not evidence.
2. **Attribute every red.** Re-run each failing id with `-n 0`. For each one that still fails, run it against
   `$L/base-export` (clean `85ebf2739`, with `PYTHONPATH` set to the export's `src` and `elspeth-lints/src`, and
   `elspeth.__file__` checked).
   - Reds that also fail on the base are pre-existing (S0 recorded 9 such at `780ef0f56`; re-measure, do not assume).
   - Reds only on the tip are S1's to fix.
   - The known flaky families (`e2e/recovery`, `integration/pipeline`, `unit/engine/orchestrator`) are diffed
     against a base run, not assumed.
3. `lints` stage: normalise its findings and compare them as a multiset with `lints-base.norm` (G-tier; never
   `comm` on the raw files). The expected additions are the
   `R_TB_SUPPRESSED` lines of `decode_wire_arguments` and any stale signed fingerprints from code moved in
   `service.py`/`tool_batch.py`/`pipeline_planner.py`/`boot_probe.py`. List them for the operator's sign-bundle, and
   stage nothing.
4. **Testcontainer is not run, and this is why:** S1 adds no DDL, no SQL, no lock, no session or Landscape
   persistence path and no epoch bump. The new data is additive JSON inside the existing `chat_messages.tool_calls`
   and `content` columns, written by the unchanged writer (`audit_storage.py`, `add_messages_atomic`). If any task
   ends up changing a column, a CHECK, a query or a lock, run `pytest tests/ -m testcontainer -n 0` before
   handover.
5. **Handover, not merge.** When the gate is green and every red is attributed, the branch is *ready for merge*.
   Stop there. Report to John with:
   - the commit list and the §5 evidence;
   - the pin moves (T2, T6, T7, T8, T9);
   - the corpus additions and the signatures owed;
   - the rulings in §7.3.
   Merge into local `release/0.8.1` only on John's word. Before merging, re-run `git merge-tree` against the current
   `release/0.8.1` tip, because other sessions merge concurrently. Run `scripts/branch-safety-check.sh --intent merge`.
   Push only when asked.
6. **Deploy condition (state it in the handover).** **Ruled (lead, provisional; John may overrule), §7.3 item 10:**
   T9 ships with S1. If S0 and S1 deploy together, the S0 baseline window (S0 acceptance 5) is taken with
   `composer_strict_tools=off`, and the strict flip is then made by changing the setting to `preferred`. The T9 repair
   signal is a stated confound: it is live in the baseline window (because `off` does not revert it), so the
   baseline measures S0 plus T9, and the `off` → `preferred` comparison isolates the strict flip only. Record the
   deploy time and the setting change time, so the §8 comparisons can be split at those boundaries. (This replaces
   the first draft's "S1 does not deploy until the S0 baseline has been recorded". If S0's baseline was already
   recorded before S1 deploys, the same `off`-first sequence still separates T9, which lands at the deploy, from the
   strict flip, which lands at the setting change.)

---

## 7. Risks, stop conditions, and rulings owed

### 7.1 Risks (S1-specific additions to master plan §6.1)

1. **Omission prose outside W (D13).** A grammar-bound model reads "omit llm_draft" in the skill and "pass null"
   in the tool description. If it sends a draft instead of `null`, the draft must byte-match or the call is
   rejected. Measure: `request_interpretation_review` ARG_ERROR / `model_validation` rate on strict routes against
   the S0 baseline (acceptance item 4).
2. **The deployed route's endpoints** may accept but ignore `strict`. Detector: `wire_conformant` by
   `provider_served` (master plan risk 2).
3. **The Responses bridge with an explicit `false`** (master plan risk 3). The boot probe proves only that it is
   accepted.
4. **The hatch probe narrows the advisor's boot share** to at least 30 s on a 4-surface boot (D9). Worst-case total
   probe time is unchanged (the 45 s shared deadline, plus the 10 s prime = 55 s). With `max_tokens` 16 (ruling 1)
   the hatch probe is far more likely to finish inside its 5 s cap than a planner-shaped request would be.
5. **Per-call `strict_tool_count` varies on the planner route** (15, 14 or up to 18 discovery tools), so compare it
   per palette, not as a constant.
6. **The test surface** (C17). Under ruling 7 the default test model keeps today's bytes, so the wire moves only in
   tests that name an OpenRouter planner or advisor, or set `forward_to_endpoint`. The T8 gate still runs the whole
   directory to find the pins, because OpenRouter models are common in the fixtures.
7. **Hosted OpenAI and Azure reach a boot-fatal strict wire that no live endpoint has accepted — now only on
   opt-in.** The Azure Container Apps bicep default is `composerModel = 'gpt-5.5'` with an empty endpoint
   (`deploy/azure-container-apps/workload.bicep:159`, `:165`): row 7, on the Responses bridge. The ACA example uses
   `azure/…` for **both** the planner and the advisor (`application.example.json:8-9`) with `AZURE_API_VERSION` from
   env (`:32-33`): row 9. Azure deployment names are arbitrary, so the model behind them is unknown, and master plan
   risk 4 (Azure strict with parallel calls) is unmeasured. **Ruled (lead, provisional; John may overrule), §7.3
   item 7, option (a):** rows 7 and 9 resolve to `NONE` under `preferred`, so these configurations keep today's bytes
   by default and first boot is unaffected. The residual risk is on opt-in: under `forward_to_endpoint` they are
   `ENFORCING`, a planner-route 400 raises `ComposerBootConfigError` (`boot_probe.py:223-227`), which the lifespan
   re-raises (`app.py:828-830`), so the first boot after opting in is that route's live acceptance test, and
   `preferred` or `off` is the remedy. The env-var doc says so (T4, T8). R8 (§8 item 2) is the measurement that
   would let the `preferred` cells move.
8. **The hatch probe could make a hatch-route failure boot-fatal.** Today the terminal reaches the advisor endpoint
   only on hatch turns (`pipeline_planner.py:4255-4262`), and a rejection fails that turn. A 400 on the hatch probe
   can be caused by the terminal schema itself rather than by `strict` (the terminal carries `not`, `pattern`,
   `propertyNames`, `anyOf` and `oneOf`, master plan §2.4, and its acceptance was measured only against ELSPETH's
   gateway), and D4's single setting would make `off` the only remedy, removing strict from the planner route too.
   **Ruled (lead, provisional; John may overrule), §7.3 item 8, option (b):** a `hatch_terminal` rejection is logged
   as `rejected` and boot continues (T8); planner-route and advisor 400s stay fatal. Residual: a rejected hatch route
   is found at boot but not blocked, so hatch turns on it fail at first use, as they would with no probe; the operator sees it only in
   the `composer_boot_probe_rejected_nonfatal` log event and the OTel counter, not on `/api/system/status` (ruling 2).
9. **The resolver can stop app construction.** T4 catches only `BadRequestError` from `get_llm_provider`, and T8
   calls it in `ComposerServiceImpl.__init__`. Any other exception from LiteLLM's resolver therefore fails app
   construction under `preferred` or `forward_to_endpoint`, even with `composer_boot_probe_enabled=false`. Today
   that path cannot fail this way: `warn_if_not_reasoning_capable` swallows every exception (`reasoning.py:67-82`).
   `off` short-circuits at row 1 before LiteLLM is called, so it is the remedy. The plan's own URL parse was a second
   such path (Codex finding 1: a malformed `OPENROUTER_API_BASE` such as `http://[` made `urlsplit` raise); D24 and
   row 3a close it, and T8's regression pins it. LiteLLM itself tolerates the malformed env values (M,
   `$L/plan-review/env_url_probe.log`).
10. **T9 and the strict flip share a deploy** and T9 is independent of the setting. Ruled: see §6 step 6 (an `off`
    baseline window with T9 as a stated confound).

### 7.2 Stop conditions (stop and report; do not improvise)

- Any route on which `strict` (`true` **or** `false`) could reach ELSPETH's gateway under `preferred`. The fidelity
  matrix's gateway row or the resolver table would show this.
- Any change to S: a diff in `test_tool_declarations.py`, or an edit to a flat definition.
- A reader found to compare a persisted `tool_calls` entry's, invocation's or LLM-call payload's key set exactly
  (for example a new one beside `provider_telemetry.py:168`, which checks only the envelope's top level). That
  would mean an epoch bump, and needs John.
- Any change that would author, choose or template pipeline structure on the server, or add a provider call to a
  compose transition (composer invariants). Decode may only strip `null` at promoted positions, unwrap the
  envelope, or reject the envelope.
- `get_tool_definitions()` turns out to depend on plugin state (T2): W would then not be static, which is the master
  plan's §1.3 premise.
- A fact that only a keyed live call can settle: bridge behaviour with explicit `false`, Azure strict with parallel
  calls, `hatch_terminal` latency at candidate effort, or whether an OpenRouter endpoint honours `strict`. Record it
  as unmeasured and continue only if the task does not depend on it.
- The whole-tree gates cannot be kept green without a suppression, a `# noqa`/`# type: ignore`, a masquerade reseed
  or a hand-edited signature.
- The full suite shows a red that is only on the tip and cannot be attributed.

### 7.3 Rulings (lead, provisional; John may overrule) and rulings still owed

Items 1-10 below were ruled by the lead on 2026-09-23 after the Codex plan review (§9); the rulings were reviewed by
Codex. Each is recorded as **Ruled (lead, provisional; John may overrule)**, the tasks are written to the ruling, and
all of them go in the handover for John. Item 11 is still owed.

1. D9: the `hatch_terminal` probe shape. This sits next to S0's open advisor-allowance ruling (45 s shared versus
   60 s alone). **Ruled (lead, provisional; John may overrule): (b)**, `max_tokens` 16 with the 5 s cap, a
   rejection check only (D9, T8). The options were:
   - **(a) As planned.** A planner-shaped request (candidate effort, 16,384 `max_tokens`) with the 5 s cap. It has
     full parity with a hatch turn, and the advisor's share becomes at least 30 s. But a reasoning model at candidate
     effort may often exceed 5 s, so on the deployed config the hatch route may stay "unverified at boot", and §8
     item 1's hatch bullet may be unprovable.
   - **(b) Acceptance-only.** `max_tokens` 16 for `hatch_terminal`, as `loop_tools` does, with the same 5 s cap. A
     400 still returns before generation, so rejection is still detected and the request is far more likely to
     finish. The cost is losing parity with the hatch turn's request shape (token cap; the effort stays).
2. D14: what the unauthenticated `/api/system/status` publishes. The first draft published per-route transport,
   effective strict counts and per-surface boot outcomes; that tells a caller whether a custom endpoint or gateway
   is configured (Codex finding 2: `preferred` + a normal OpenRouter host gives 32 strict tools, the same model + a
   custom endpoint gives 0) and whether each provider was reachable at boot. **Ruled (lead, provisional; John may
   overrule):** publish only closed, route-independent values, the setting and a clearly named strict-**capable**
   tool count (32 of 42, a property of the tool set, not of the route). Per-route transport, effective strict counts
   and per-surface boot outcomes go to structured logs only. A test pins identical public payloads for
   otherwise-identical normal-OpenRouter-host and custom-endpoint configs (T10). Policy note: the
   logging-telemetry skill routes config-lifecycle events to telemetry; S1 follows the web boot path's `slog`
   precedent as the ruling names (T8).
3. D1: the compose-loop wire facts on the assistant `tool_calls` entries, not on the tool-row content. **Ruled
   (lead, provisional; John may overrule): accepted.**
4. D13: omission prose outside W left unchanged; revisit it with the risk 1 measurement. **Ruled (lead,
   provisional; John may overrule): unchanged.**
5. D6: unify the OpenRouter detectors (reasoning, branding, usage) as a follow-on. **Ruled (lead, provisional; John
   may overrule): follow-on, not S1.**
6. Still open from S0 and not changed by S1: `RejectionRecord.error_code` on ARG_ERROR rows. **Ruled (lead,
   provisional; John may overrule): S1 leaves `RejectionRecord` untouched.**
7. Risk 7 (first boot of the default and Azure configs). **Ruled (lead, provisional; John may overrule): (a)**, for
   hosted OpenAI as well as Azure: rows 7 and 9 resolve to `NONE` under `preferred` until a live measurement (R8),
   and to `ENFORCING` only under `forward_to_endpoint`. Under the default, S1's wire change reaches OpenRouter routes
   only. T4's truth table and expected values, T8's default-settings fixtures, T10 and T11 are written to it, and
   every test expectation that assumed the default test model resolves to `ENFORCING` was rewritten (C17). The
   options were:
   - **(a)** Azure resolves to `NONE` under `preferred`, and becomes `ENFORCING` only under `forward_to_endpoint`,
     until R8 has measured it (a table change in T4, rows 9 and 10). The hosted-OpenAI default (row 7) could be
     treated the same way.
   - **(b)** Keep the table as designed (R4 as adopted), with the env-var doc sentence already planned, plus an
     `off` note in the ACA runbook's upgrade notes.
8. Risk 8 (the hatch probe is boot-fatal and shares one remedy with the planner). **Ruled (lead, provisional; John
   may overrule): (b)**, a `hatch_terminal` probe rejection is non-fatal (logged as `rejected`, boot continues). The
   non-fatal handling is surface-specific: planner-route and advisor 400s stay fatal as in S0 (T8). Option (b)'s
   "show it on `/api/system/status`" clause is overridden by ruling 2: the outcome goes to the structured log and the
   OTel counter only. The options were:
   - **(a)** Accept it as the trade.
   - **(b)** Keep D4, but make a `hatch_terminal` rejection non-fatal: log it as `rejected`, show it on
     `/api/system/status`, and keep booting.
   - **(c)** Add the per-role override that D4 defers.
9. D19: should the repair signal also reach planner discovery (extend `_ArgumentErrorResponse` with the closed
   loc) and MCP session tools (extend the MCP error text)? Both are contract changes on surfaces S1 leaves alone.
   **Ruled (lead, provisional; John may overrule): no**, the repair signal stays compose-loop only.
10. T9's deploy: ship it with S1, or split it into its own deploy so the strict flip and the repair signal can be
    measured apart. **Ruled (lead, provisional; John may overrule): T9 ships with S1.** If S0 and S1 deploy
    together, the S0 baseline window is taken with `composer_strict_tools=off`, and the T9 repair signal is a stated
    confound (§6 step 6, which replaces the first draft's "S1 does not deploy until the S0 baseline has been
    recorded").
11. Still owed: the review-round decisions D16-D23 and the Codex-round decision D24 (§1.3) are the plan's own
    choices; any of them can be overruled.

---

## 8. Acceptance items that need a dev deployment (listed, not done here)

1. Boot on the dev deployment (OpenRouter deepseek planner, OpenRouter glm hatch):
   - `loop_tools` with 32 `strict:true` + 10 `false` is accepted;
   - `planner_tools` (19 + terminal `false`) is accepted;
   - `hatch_terminal` (terminal `false`, `max_tokens` 16) is accepted on the hatch route; a rejection would show as
     a `composer_boot_probe_rejected_nonfatal` event, not a failed boot (ruling 8);
   - record `provider_served` for each, the logged total probe time, and the `hatch_terminal` latency (a 16-token
     acceptance request at candidate effort, ruling 1; it measures acceptance, not a hatch turn's latency);
   - the `composer_tool_contract_resolved` event shows both routes `forwarding`, and one
     `composer_boot_probe_outcome` event per surface shows `success`; `/api/system/status` shows only
     `{setting: "preferred", strict_capable_tool_count: 32, tool_count: 42}` (ruling 2).
2. If an Azure or OpenAI deployment is available (R8): set `composer_strict_tools=forward_to_endpoint` (under
   ruling 7, `preferred` sends these routes today's bytes), confirm the same probe is accepted on `ENFORCING`, and
   make one live call showing whether explicit `strict:false` changes the bridge's behaviour. This is the
   measurement that would let ruling 7's `preferred` cells move.
3. After a comparable window, compare against the S0 baseline, split at the recorded deploy and setting-change
   times (§6 step 6; if S0 and S1 deployed together, the baseline is the `off` window and includes T9):
   - runtime `_BadRequestLLMError` by `provider_served`;
   - the `wire_conformant=False` rate per tool on the 32, by `provider_served`, on rows with `strict_sent` true
     (Appendix A with the T12 columns);
   - ARG_ERROR categories on the 32 (T9 changes repair text in the same deploy; with an `off` baseline window the
     `off` → `preferred` split isolates the strict flip, and T9's own effect is confounded with S0's in that
     baseline, so say so);
   - first-call latency.
   State the result plainly, even if nothing measurable changed.
4. The risk 1 measurement: `request_interpretation_review` ARG_ERROR / `model_validation` rate on strict routes
   against the S0 baseline.
5. The S0 items still owed (S0 acceptance 2, 4 and 5). S1's boot run (item 1) also covers S0 acceptance 4.
6. The operator fires a sign-bundle for the S0a +4 and S1's stale fingerprints before merge (agents do not stage it).

---

## 9. Plan review

Three critiques were run against the first draft of this plan at `85ebf2739`: a reality check of every citation and
count, a test-strategy review and a risk and invariants review. Each finding was re-checked against the code before
it was applied. Of 48 findings, 7 repeated another critic's finding, leaving 41 distinct ones. All 41 held up and
were applied, 4 of them in part; no finding was rejected outright. The 4 rejected sub-proposals were: required
`begin_dispatch*` keywords (25 test call sites; a parity pin covers the gap instead), a required `env` on the probe
builder (it would edit the gateway test file, whose append-only diff is an invariant), a root-conftest env scrub
(live provider tests read Azure settings from env), and diffing call counts against the base export (a
cross-dialect comparison on the tip tests the same property). The dispositions, with the reason for each, are in
the lane (`$L/revise-dispositions.md`).

What changed:

- **Defects that would have stopped execution.** T2's faithfulness check 6 would have raised at import, because
  `upsert_node` already has two `null`-bearing enums in S (C20); it now applies to strict-capable W only. T1's ported
  controls used kinds its closed set did not have, and one negative used `title`, which the S0 allowlist does not
  allow; T1 now publishes the kind vocabulary, the rewritten expected sets and a `title`-free negative (D23). T9 named
  MCP and planner-discovery tests that its code change cannot reach, and four pin moves that do not move; it is now
  scoped to the compose loop, with the other surfaces pinned as unchanged (D19).
- **A broken instrument.** The trust-tier corpus base is unsorted and carries line numbers, so the planned `comm`
  gave thousands of false differences. G-tier now uses a normalised multiset `diff`, with three recorded controls.
- **Specification gaps.** Decode for a tool name outside the sent list is now defined (D17), which matters on the
  planner because its palette is not enforced at dispatch (C21). Ledger text on description-less `items` has a home
  (D22). `strict_sent` is three-valued so "no key" and "explicit false" stay apart (D16). The wire facts are passed at
  every dispatch site, with a parity pin (D21). The new carrier fields default to `None`, so 22 existing test
  constructions are untouched. The service and the probe share one resolution helper (D20). Encode's null branch
  moves to S2, where it gets its first reader (D18).
- **Weak or tautological tests.** The resolver drift pin compared LiteLLM with itself; it now uses a literal
  provider column. The NONE-route byte pin hashed a list production never sends and could not see tuple versus list;
  the two production palettes are now hashed, and the durable pin is a relational, type-sensitive equality. The
  probe/production parity test now covers the planner list and the hatch terminal. The "no provider call added"
  check is now behavioural. Env variables that steer the resolver are scrubbed in the unit and web-integration
  conftests. The cost-map precondition is made to hold on purpose in the root conftest (C24). RED reasons,
  characterization tests and mutation controls are named honestly.
- **Risks and rulings made visible.** The first boot of the default and Azure configurations, the boot-fatal hatch
  probe, the resolver failing app construction, and T9 sharing a deploy with the flip are now in §7.1, with rulings
  in §7.3 (items 7-11) and a deploy condition in §6 step 6. D14's rationale was corrected to state what the status
  endpoint newly discloses.

### Codex plan review

After the three critiques, Codex reviewed the revised plan read-only at `29e1b78ee` (verdict "yes with fixes"; the
review is in the lane, `$L/codex/codex-plan-review.md`). Codex reproduced the NONE-route baselines (loop 42 tools /
63,872 B / `6c7f60dd…`, planner 20 tools / 29,264 B / `7e958245…`) and found the decode, audit and gate design sound.
Each of its six findings was re-checked against the code before it was applied; all six held and were applied.

| # | Severity | Finding | Checked against | Applied as |
|---|---|---|---|---|
| 1 | Major | T4/T8: a malformed env base (`OPENROUTER_API_BASE=http://[`) made the plan's `urlsplit(base).hostname` raise, and T8 runs the resolver in `ComposerServiceImpl.__init__` even with probes disabled, so boot would fail | M, `$L/plan-review/env_url_probe.log`: `urlsplit('http://[')` raises `ValueError: Invalid IPv6 URL`; `'not-a-url'` gives `hostname None`; LiteLLM's `get_llm_provider` tolerates all three malformed env variables. Settings bases are validated (`config.py:145-175`, R); env bases are not | D24, T4 row 3a and `StrictTransportResolution` with a closed diagnostic, T4 malformed-env rows, T8 service-construction regression with probes disabled, risk 9 |
| 2 | Major | T10/D14: publishing the effective strict count still reveals routing (normal OpenRouter host 32, custom endpoint 0) | R: T4 rows 4-5, T10's "count computed from the stamped list" | Ruling 2: public payload is the setting plus the route-independent `strict_capable_tool_count` and `tool_count`; per-route facts and boot outcomes to structured logs; T10 pins identical payloads for the two configs |
| 3 | Minor | T8: the hatch-routing mutation cannot go red, because the parity fixture's planner and hatch both resolve `OPENAI_STRICT` | R: `openrouter/deepseek/…` and `openrouter/z-ai/glm-5.3` are both row 4; `supports_anthropic_prompt_cache_markers` is `False` for both (M, `$L/plan-review/d8_probe.log`) | New T8 asymmetric-route test in both directions (row 4 vs row 5 on each side), asserting surface inclusion/omission, effective endpoint and transmitted flags; the planner-dialect substitution goes red in both |
| 4 | Minor | T11: forwarding fixtures omit the setting that forwarding needs (loopback routes are rows 5 and 8, `NONE` under `preferred`); with ruling 7 hosted OpenAI and Azure rows need it too | R: T4 table | T11 rows use `forward_to_endpoint` explicitly and assert the resolved transport first; separate `preferred` controls send no `strict` key; T8 default-settings fixtures moved to an OpenRouter planner |
| 5 | Minor | T6: `_replace_llm_tool_call_arguments` has no dialect and no `ctx` in scope | R: `tool_batch.py:450-485` takes `llm_messages`, `tool_call_id`, `arguments` only; all 7 callers are inside `run_tool_batch` (`:686`) | Required keyword-only `dialect` (and `semantic`), passed by all 7 production callers as `ctx.tool_contract_dialect` and by both test callers; byte-identical semantic/sentinel transcript pins for both dialects |
| 6 | Minor | T4: "edit one literal" mutates the oracle, not the thing under test | R: plan text; M, `$L/plan-review/drift_mutation_probe.log` (`gpt-5.5` + `api.deepseek.com` → `deepseek`, + `api.openai.com` → `openai`) | Drift pin now reads `_routing_provider`; controls mutate the routing input (deepseek base → OpenAI base) and the resolution result (monkeypatched `get_llm_provider`), never the literal |

The lead ruled on §7.3 items 1-10 in the same round (Codex reviewed the rulings and found them consistent with S1's
scope). Each is recorded in §7.3 as **Ruled (lead, provisional; John may overrule)**, and the tasks are written to
it:

- **Item 1 (D9):** `hatch_terminal` sends `max_tokens` 16 with the 5 s cap, a rejection check only.
- **Item 2 (D14):** `/api/system/status` publishes only the setting and the strict-capable tool count (32 of 42) and
  the tool count; per-route transport, effective counts and boot outcomes go to structured logs (Codex finding 2).
- **Items 3-6:** D1 accepted; D13 unchanged; D6 a follow-on; `RejectionRecord` untouched.
- **Item 7:** option (a). Hosted OpenAI and Azure resolve to `NONE` under `preferred` until R8, and to `ENFORCING`
  only under `forward_to_endpoint`, so the default wire change reaches OpenRouter routes only. T4's truth table and
  expected values, T8's fixtures and pin moves, T10, T11, C17, risks 6 and 7 and §8 item 2 were rewritten.
- **Item 8:** option (b). A `hatch_terminal` rejection is non-fatal and surface-specific; planner-route and advisor
  400s stay fatal (T8 code and test). The option's "show it on the status endpoint" clause yields to item 2.
- **Item 9:** the repair signal stays compose-loop only (D19, T9).
- **Item 10:** T9 ships with S1; if S0 and S1 deploy together, the S0 baseline window is taken with
  `composer_strict_tools=off`, and T9 is a stated confound (§6 step 6, T9, risk 10, §8 item 3).

Two corrections found while applying them: D13 cited "§7.1, risk 4" for the omission-prose risk, which is risk 1;
and the T8 hermeticity control (`OPENAI_BASE_URL` on the default `gpt-5.5`) could not discriminate under ruling 7,
so it now uses `OPENROUTER_API_BASE` on an OpenRouter planner, plus a transport-level check under
`forward_to_endpoint`.
