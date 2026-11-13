# Spike S4: cross-model reproduction (Sonnet 5.5 and GPT-5.6 Luna as composer)

Date: 2026-11-13. Code: `main` at `c987b2b2` (no `src/` change since the audit
baseline `1a7ff0e6`; `git diff --stat 1a7ff0e6..HEAD -- src` is empty).
Raw files: `.claude/lanes/composer-desk-audit-20261112/spikes/S4-cross-model/`
(called `S4/` below).

> **The spike is incomplete.** The shared OpenRouter account ran out of credit
> at about 08:43:40Z, partway through the third scenario
> (`/api/v1/credits`: `total_credits 1240`, `total_usage 1240.76`). From then
> on every call failed with HTTP 402. On Sonnet the calls asked for 65,536
> output tokens and the account "can only afford 48643". On GLM they asked for
> 8,192. I stopped both drivers and both instances. **s01, s10 and r01 ran on
> neither model, and t01 did not run on Luna.** So F1, F3, F8 and F9, and most
> tool-loop behaviour, are NOT RUN (credit exhausted). Most of the results below are about
> the planner surface.

## 1. Question and why it matters

Every rate in the audit comes from one composer, DeepSeek v4.1 Flash. The
demo's composer model has not been tested. The questions:

- Which findings are product defects that will reproduce on other composer
  models, and which are DeepSeek quirks?
- Does the composer work at all on an Anthropic-family and an OpenAI-family
  model over OpenRouter? The risks are cache markers, thinking replay and
  strict tool schemas.

This matters for the acceptance criteria of every lane that cites a rate,
especially CDA-07 ("on the demo model pair"), CDA-06/CDA-18 (F4) and CDA-16
(caching). It also matters for the demo-day choice of model.

## 2. Method

- **Instances** (EXECUTED), each started with `S4/start_instance.sh`:
  - port 8521: `openrouter/anthropic/claude-sonnet-5.5`;
  - port 8522: `openrouter/openai/gpt-5.6-luna`.
- Both model ids are listed on `/api/v1/models` (`S4/models.json`).
- The advisor stayed `openrouter/z-ai/glm-5.3`.
- `ELSPETH_WEB__LLM_PROFILES` used the `instance.sh` default: the `standard`
  profile, DeepSeek v4.1 Flash.
- The instance state is in `evals/composer-live-audit/runs/instances/S4-cross-model-{sonnet,luna}/`,
  which is gitignored. Captures are in `capture/llm_calls.jsonl` and logs in
  `web.log`.
- **Driver.** `S4/run.sh` is `run_scenarios.sh` with `--out` pointed at
  `S4/sessions/<model>/`. It does not overwrite the audit's shared
  `runs/sessions`, `digest.md` or `register.md`. Each session was run once
  (n=1), and turns were auto-accepted as in the audit.
- **Planned:** p01, s04, s06, t01, r01, s10 and s01 on both models.
- **Ran** before credit ran out:

  | Model | Scenarios | Turns | Usable turns |
  |---|---|---|---|
  | Sonnet | p01, s04, s06, t01 | 14 | 12 (s06 T2 and T3 failed on 402) |
  | Luna | p01, s04, s06 | 12 | 11 (the s06 T2 advisor calls failed on 402) |

- **Instruments.**
  - `turn_table.py` over each model's sessions: `S4/turn-table-{sonnet,luna}.{md,json}`.
  - `S4/analyze.py` gives a per-turn table with cost: `S4/turns.json` and
    `S4/turns-table.txt`.
  - `S4/guard_probe.py` runs the production `require_no_credential_material`
    over every provider-authored field in a capture.
    - Positive control: on the audit's DeepSeek capture (`live/capture/llm_calls.jsonl`)
      it finds the 4 known s06 reasoning hits (seq 32 and 34).
    - Negative control: `"Hello world"` does not hit; `"password: hunter2"`
      does (`S4/guard_label_probe.txt`).
  - Cost is OpenRouter's own `usage.cost` per call (`S4/cost.py`, output in
    `S4/cost.txt`).
- **Spend** (from captures; 402 calls cost $0):

  | Model | Composer | Advisor (GLM) | Total |
  |---|---|---|---|
  | Sonnet | $1.30 | $0.25 | $1.55 |
  | Luna | $0.10 | $0.32 | $0.42 |
  | **Total** | | | **$1.97** |

  The account `usage` went from 800.70 to 803.79 over the same window. That
  delta includes other spikes.

## 3. Results

### 3.1 Boot (EXECUTED, `web.log` of each instance)

| Boot line | DeepSeek (audit `tb1`) | Sonnet 5.5 | GPT-5.6 Luna |
|---|---|---|---|
| Composer "not reasoning-capable per litellm's registry" warning | yes | **no** | **no** |
| Advisor (GLM) not-reasoning-capable warning | yes | yes | yes |
| `composer_tool_contract_resolved`, planner | `openai_strict`/forwarding | **`none`/none** | `openai_strict`/forwarding |
| Loop strict count | 35 of 45 | **0 of 45** | 35 of 45 |
| Hatch (GLM) | `openai_strict` | `openai_strict` | `openai_strict` |
| Boot probe | skipped (debug mode) | skipped; tools `strict_key_omitted=45/23` | skipped |
| `composer_turn_budget_underfunded` (34 configured / 16 fundable) | yes | yes | yes |

Sonnet gets no strict tools. That is by design: in
`strict_transport.py`, row 2 (D8) says "cache markers and strict never share a
route" (`supports_anthropic_prompt_cache_markers` → `NONE`). No boot line
reports whether cache markers are on; the only way to see them is in the
captured messages.

### 3.2 Transport behaviour (EXECUTED from captures)

**Strict schemas, Luna.** 19 of 19 composer calls were accepted, on both the
strict planner palette and the strict tool loop. There were no schema
rejections.

- In s06 T2 the loop calls were `patch_output_options`, `upsert_node` ×2,
  `patch_source_options` and `preview_pipeline`, all successful.
- The GLM hatch never ran, so the strict path on the hatch is NOT EXERCISED.

**Thinking replay, Sonnet.**

- OpenRouter returns reasoning as `provider_specific_fields.reasoning_details`
  entries with an opaque `signature` (`format: anthropic-claude-v1`).
- LiteLLM gives no `thinking_blocks`, so the composer replays assistant
  tool-call turns without any thinking (`provider_gateway.py:523` replays only
  `thinking_blocks`).
- Anthropic, via OpenRouter, **accepted every such follow-up**: planner seq
  3 and 24, tool-loop seq 26 and 27. Reasoning was on in all of them
  (`reasoning.effort` low or high).
- No replay refusal was seen. Two consequences:
  - The thinking signatures are recorded for audit but never replayed.
  - The D1 rule "a hit in replayed `thinking_blocks` still fails" never applies
    on this route.

**Prompt cache, Sonnet** (explicit markers):

| Surface | Cached / prompt | Why |
|---|---|---|
| Tool loop, warm | 95,957 / 102,602 (seq 26); 102,600 / 104,058 (seq 27) | Markers on the system message, the catalog message and the history tail; works |
| Planner (every call) | **58,977 / ~79,500 = 74%** | The planner's second message is not marked |

The unmarked planner message is the 57,476-char
`{"authoring_aids":{"discovery_digest":…}}` block.

- It does not start with `CATALOG_CONTEXT_PREFIX`, so
  `apply_anthropic_cache_markers` (`llm_response_parsing.py:943-958`, called
  at `pipeline_planner.py:3600`) leaves it unmarked.
- It is byte-identical across all 16 Sonnet planner calls and 4 sessions:
  sha256 prefix `05911576` of the message content, computed from the
  captured requests in the instance's `capture/llm_calls.jsonl`.
- So about 20k tokens are re-read uncached at $2/M on every planner call.
  That is about $0.040 of the measured ~$0.050 per Q&A planner call.

**Prompt cache, Luna** (automatic). Planner Q&A calls cached 35,752 of about
47.7k tokens (75%) across sessions. Follow-ups within a session reached
47,453 of 48,358 tokens.

**Advisor (GLM).** 2 of 22 advisor calls missed the cache (10 cached tokens).
Each cost $0.068–0.072, against $0.015–0.025 for a call that hit. This is
relevant to CDA-16.

**Empty response, Sonnet.** One call returned empty content with
`finish_reason=tool_calls` and no tool calls: p01 T1, seq 1, effort `low`,
27 output tokens.

- The planner recovered with `prose_nudged` at the cost of one extra call.
- This is 1 of 19 successful Sonnet composer calls.

**Credit-limit sensitivity.** The tool loop reserves `max_tokens=65536`; the
planner reserves 16,384.

- On Sonnet that is about $0.66 of headroom per tool-loop call.
- As the account ran down, Sonnet tool-loop calls failed with 402 while the
  cheaper calls still went through.
- The user saw `502 llm_unavailable` (s06 T2 and T3).

### 3.3 Per-turn measurements (EXECUTED; raw: `S4/turns-table.txt`)

Composer cost excludes the advisor. The advisor (GLM) adds about
$0.015–0.07 per turn on every model.

| Measure | DeepSeek (audit, same turns) | Sonnet 5.5 | Luna |
|---|---|---|---|
| Q&A planner turns, latency p50 (nearest rank) | 8.3 s (n=10, p01 + s04) | 6.4 s (n=9 completed) | 7.3 s (n=10) |
| Q&A planner turns, composer $ per turn | $0.001–0.003 | $0.049–0.054 (T1 with cache write and empty response: $0.29) | $0.0038–0.0043 |
| Composer prompt tokens per call | ~47–54k | ~79k (Anthropic tokenizer) | ~47.7k |
| Proposal turns | t01 19.3 s, $0.030; s06 T1 failed (F4) | s06 T1 17.3 s, $0.119; t01 11.4 s, $0.137 | s06 T1 43.6 s, $0.027 |
| `emit_pipeline_proposal` first-try acceptance | 27 of 40 in the battery | 2 of 2 | 0 of 1 (rejected `node_input_not_reachable`/`transform_on_success_dangling`, then `plugin_options_invalid`, then accepted) |
| Provider/transport errors not caused by credit | none | none (1 empty response, recovered) | none |

### 3.4 Finding × model

The DeepSeek column is the audit's. Turn and evidence paths are under
`S4/sessions/<model>/<scenario>/turnNN/`.

| Finding | DeepSeek v4.1 Flash | Sonnet 5.5 | GPT-5.6 Luna | Model-independent? |
|---|---|---|---|---|
| **F1** two files on a new session | REPRODUCES (s10) | NOT RUN (credit exhausted) | NOT RUN (credit exhausted) | Yes by code (READ: schema/custody gap, README F1); no cross-model live evidence |
| **F2** advisor withholds correct "not available" answers | 4 of 6 withheld | **DOES NOT, 0 of 3**: s04 T2 SharePoint, p01 T2 PDF, p01 T3 Teams all admitted CLEAN | **REPRODUCES, 2 of 2**: s04 T2 SharePoint, p01 T3 Teams withheld. Advisor: "no list_sinks… invocation appears… never a bare assertion" | **Yes.** The mechanism is identical. The rate depends on phrasing: Sonnet names the available alternatives, Luna writes "I don't see a … connector" |
| **F2, converse** (new) | not seen | not seen | **REPRODUCES:** p01 T2 "Yes. I can read PDFs when you upload one…" is false (no PDF plugin) and the advisor returned CLEAN/ADMIT | **Yes.** An advisor with no catalog can neither confirm a denial nor refute a false capability claim |
| **F3** `diff_pipeline` without a baseline | 4 of 4 | NOT RUN (credit exhausted) | NOT RUN (credit exhausted) | Yes by code (READ, `tools/generation.py:4659-4668`) |
| **F4** credential guard fails the turn (reasoning) | 2 of 2 (s06 T1, T2) | DOES NOT: s06 T1 accepted; guard probe found 0 hits in 19 calls' reasoning | DOES NOT: s06 T1 accepted; 0 hits | The reasoning variant is DeepSeek-specific on this n. The guard is not (next row) |
| **F4b, content** (new) | not seen | **REPRODUCES:** s04 T5 "Is this safe to run on sensitive data?" gave 422 `credential_material_rejected`, `token_shape`, surface `composer_planner_response` | DOES NOT (s04 T5 admitted) | **Yes.** The detector itself; see 3.5 |
| **F5** model selection by slug / unfiltered `list_models` | 3 of 3 misleading | **REPRODUCES:** p01 T1 offers "A specific model from the catalog. The catalog lists 3,972 models…" and says the profile's model "isn't exposed here" (D4 says it should be) | **REPRODUCES:** p01 T1 lists the litellm provider dump ("Fal.ai: 943 models…") | **Yes** (tool result and skill text) |
| **F8** receipt on every tool-loop reply | 29 of 29 | NOT RUN (credit exhausted; no completed loop turn) | NOT RUN cleanly (credit exhausted). s06 T2, advisor down: the saved-scope sentence appears **twice**, as in s09 T2 | Yes by code (backend text). The duplication also occurs on the advisor-unavailable withhold path |
| **F9** "Correction to my previous message" | 2 seen | NOT RUN (credit exhausted): s06 T2 advisor FLAGGED, and the repair call got a 402 | NOT RUN (credit exhausted) | Unknown |
| **F10** "emit now" notice | 29 of 41 | **REPRODUCES:** on the informational p01 T1 (3 copies by the 5th request), on s06 T1 and on t01 | **REPRODUCES:** p01 T1, s06 T1 | **Yes** (server notice) |
| **F17** internals or "file a bug" | 0 in the battery | DOES NOT (0 of 11 completed replies) | DOES NOT (0 of 10 completed replies) | No evidence either way (n small) |
| **F18** tutorial precondition: suggestion offered; discard with one json output | 3 of 3 | **REPRODUCES:** t01 has the "Consider adding error routing…" suggestion, 1 `json` output, all `discard` (`state.json`) | NOT RUN (credit exhausted) | **Yes** (`state.py:10079` suggestion; frontend Apply) |
| Planner: empty first reply | not seen | 1 of 19 calls | not seen | Model quirk; handled by `prose_nudged` |

### 3.5 F4b, the credential guard rejects ordinary prose labels (EXECUTED)

The rejected Sonnet reply was ordinary advice. It contained the line
`- **Credentials:** Credentials and model bindings are managed by your operator. I never see secret values.`
Minimising it with the production detector (`S4/guard_label_probe.txt`):

| Text | Result |
|---|---|
| `- **Credentials:**` | HIT `credential_assignment` |
| `Credentials: managed by your operator.` | HIT |
| `**Password:** never shown` | HIT |
| `credentials: **` | HIT |
| `**Secrets:** …`, `**API keys:** …`, `**Passwords:**`, `## Credentials:`, `password:` | no hit |
| `password: hunter2` (positive control) | HIT |
| `Hello world` (negative control) | no hit |

**Cause.** The `credential_assignment` pattern
(`contracts/credential_material.py:85`, `CREDENTIAL_PATTERN_SPECS`) accepts
any run of non-space characters as the value. That includes the markdown
emphasis `**` and the plain word `managed`.

**Scope.**

- It fires on visible reply content and on tool arguments. D1/CDA-18 only
  covers reasoning, so the turn still fails.
- The user reads "This control content appears to contain a credential…"
  after asking a security question. That question is already one of the
  audit's runbook risks.
- Any model that writes "**Credentials:**" or "Password: …" as a label in a
  safety answer will trip it.
- The same pattern table is used by `web/validation.py:180`.

## 4. Recommendation for the lanes

**Treat as model-independent** (fix regardless of the demo model): F2 and its
converse, F4b, F5, F10, F18, and the F8 duplication. F1 and F3 stay
model-independent by code; this spike adds no live evidence for them.

**Model-dependent on this evidence:**

- F4's reasoning variant (DeepSeek drafting sample secrets in reasoning);
- the F2 withhold rate (phrasing);
- the Sonnet empty-response quirk.

Proposed edits to `work-packages.md` (stated, not made):

1. **CDA-07 acceptance.**
   - Replace "A planted false capability claim is withheld." with: "A
     planted false capability claim is withheld, and the natural case from
     S4 (Luna, p01 T2: 'Yes. I can read PDFs when you upload one') is
     FLAGGED when replayed against the capability record."
   - Add: "Run the acceptance on at least two composer models (the demo model
     and one other). The withhold rate is phrasing-dependent: 0 of 3 on
     Sonnet, 2 of 2 on Luna, 4 of 6 on DeepSeek."
2. **New fix-list row and decision for F4b** (content false positive). This
   is not covered by D1.
   - **Proposed lane, CDA-19.** Narrow `credential_assignment` so the value
     must be a credential-shaped token: not purely markdown or punctuation,
     and not followed by further prose words.
     - Or keep the pattern, and on surface `composer_planner_response` or the
       tool-loop reply re-prompt the composer once to rephrase, instead of
       failing the turn.
   - Tests in `tests/unit/web/test_credential_guard.py` take the rows of the
     §3.5 table as fixtures. `password: hunter2` must still hit.
   - It touches `contracts/credential_material.py`, which is shared with
     `web/validation.py`, so it needs the maintainer's call
     (**proposed D6**). It conflicts with CDA-06 and CDA-18 on
     `test_credential_guard.py`; schedule it after CDA-18.
3. **CDA-08.** Add to acceptance: "the receipt is never duplicated, including
   on the advisor-unavailable withhold" (S4 Luna s06 T2).
4. **CDA-16** (cache investigation). Add a finding and a trivial fix
   candidate:
   - On Anthropic routes the planner's `authoring_aids` message is byte-stable
     but unmarked, so about 20k tokens are uncached per planner call.
   - Mark it in `apply_anthropic_cache_markers`: either recognise its
     `{"authoring_aids":` header or give it `CATALOG_CONTEXT_PREFIX`.
   - The marker budget allows it: the planner uses 2 of 4 (system and last
     tool).
   - Expected planner hit about 99%, and a Q&A planner call from about $0.050
     to about $0.011. This is INFERRED from the measured token split and list
     prices; measure it after the change.
   - The test goes in `tests/unit/web/composer/test_provider_cache_markers.py`.
   - Also record that 2 of 22 GLM advisor calls missed the cache and cost
     about 3× as much.
5. **CDA-11 / CDA-12** (F5). No change. Both models reproduce F5 at p01 T1,
   which confirms that the fix is the tool result and skill text, not the
   model.
6. **§6 re-run.** Run the post-wave-2 battery on two models, and fund the
   OpenRouter account first. Sonnet tool-loop calls reserve 65,536 output
   tokens, about $0.66 each, and fail with 402 (`llm_unavailable`) well
   before the balance reaches zero.

**Demo composer?** Not on this evidence. **Luna is worse than DeepSeek**: it
made a fabricated PDF capability claim that the advisor admitted, its denials
were withheld 2 of 2, and its first proposal was rejected twice (43.6 s).
**Sonnet looks stronger on the planner:**

- its proposals were accepted first try (2 of 2);
- Q&A p50 was 6.4 s against DeepSeek's 8.3 s;
- 0 of 3 of its denials were withheld.

Against that:

- it failed the "is this safe?" turn on F4b;
- its composer cost is about $0.05 per Q&A turn and $0.12–0.14 per proposal
  turn, against DeepSeek's $0.001–0.003 and $0.016–0.030 (30–50×). After the
  CDA-16 marker fix, a Q&A planner call would be about $0.011 (INFERRED);
- its edit loop (s01) and multi-file case (s10) were never measured.

Do not switch the demo model until F4b is fixed and s01, s10 and r01 have
run on Sonnet.

## 5. Open questions

What would change this recommendation:

- **Missing scenarios.** s01, s10 and r01 on both models, and t01 on Luna.
  They decide F1, F3, F8, F9 and tool-loop quality.
  - The rest of the budget is about $4–5 for Sonnet and about $1 for Luna at
    the measured rates.
  - Resume only after the account is funded (check
    `curl -s https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OPENROUTER_API_KEY"`
    shows `total_credits` above `total_usage`). From the main checkout:

    ```bash
    S4=.claude/lanes/composer-desk-audit-20261112/spikes/S4-cross-model
    $S4/start_instance.sh 8521 S4-cross-model-sonnet anthropic/claude-sonnet-5.5
    $S4/start_instance.sh 8522 S4-cross-model-luna openai/gpt-5.6-luna
    # The s06 directories are overwritten on a re-run; earlier s06 output is in S4/turns.json.
    $S4/run.sh 8521 S4-cross-model-sonnet sonnet s06-errors r01-feedback-repeat s10-two-files s01-orders-edits > $S4/run-sonnet-2.out 2>&1 &
    $S4/run.sh 8522 S4-cross-model-luna luna s06-errors t01-tutorial-brief r01-feedback-repeat s10-two-files s01-orders-edits > $S4/run-luna-2.out 2>&1 &
    # afterwards: stop both launch_web.py PIDs printed by start_instance.sh, then
    python3 $S4/analyze.py sonnet=$S4/sessions/sonnet=evals/composer-live-audit/runs/instances/S4-cross-model-sonnet/web.log \
      luna=$S4/sessions/luna=evals/composer-live-audit/runs/instances/S4-cross-model-luna/web.log > $S4/turns.json
    python3 $S4/cost.py evals/composer-live-audit/runs/instances/S4-cross-model-*/capture/llm_calls.jsonl
    ```
- **Sample size.** n=1 per turn. The F2 rates (0 of 3 against 2 of 2) and the
  F4b trip could easily differ at n=3. The F4b detector behaviour itself is
  deterministic (§3.5).
- **Thinking replay.** Whether Anthropic would refuse it on a direct (non-OpenRouter)
  route that populates `thinking_blocks` is not measured. Bedrock or direct
  Anthropic would exercise the replay path and the D1 carve-out.
- **Strict hatch.** The GLM escape hatch with strict tools never ran on either
  instance (F19 path).
