# Spike S4: cross-model reproduction (Sonnet 5.5 and GPT-5.6 Luna as composer)

Date: 2026-11-13. Code: `main`. Phase 1 ran at `c987b2b2` and phase 2 at
`c110c242`. Neither has a `src/` change since the audit baseline `1a7ff0e6`:
`git diff --stat 1a7ff0e6..HEAD -- src` is empty at both.
Raw files are in `.claude/lanes/composer-desk-audit-20261112/spikes/S4-cross-model/`
(called `S4/` below).

**Status: complete.** Every planned scenario ran once on both models.

- **Phase 1** (08:40Z) ran p01, s04, s06 and t01. It stopped when the shared
  OpenRouter account ran out of credit.
- **Phase 2** (13:24Z, after a top-up) ran t01 on Luna, then s10, r01 and s01
  on both models.
- Two phase-1 turns are contaminated by HTTP 402 errors and are excluded from
  the rates: Sonnet s06 T2–T3 and Luna s06 T2.

## 1. Question and why it matters

Every rate in the audit comes from one composer, DeepSeek v4.1 Flash. The
demo's composer model has not been tested. The questions:

- Which findings are product defects that will reproduce on other composer
  models, and which are DeepSeek quirks?
- Does the composer work at all on an Anthropic-family and an OpenAI-family
  model over OpenRouter? The risks are cache markers, thinking replay and
  strict tool schemas.

The answer sets which lane acceptance criteria are model-independent. It
matters most for CDA-07, which says "on the demo model pair". It also affects
CDA-06/18/19 (credential guard) and CDA-16 (caching), and the demo-day choice
of model.

## 2. Method

- **Instances** (EXECUTED), each started with `S4/start_instance.sh`:
  - port 8521: `openrouter/anthropic/claude-sonnet-5.5`;
  - port 8522: `openrouter/openai/gpt-5.6-luna`.
- Both model ids are listed on `/api/v1/models` (`S4/models.json`).
- The advisor stayed `openrouter/z-ai/glm-5.3`.
- The pipeline LLM profile was the `instance.sh` default: `standard`, DeepSeek
  v4.1 Flash.
- The instance state is in `evals/composer-live-audit/runs/instances/S4-cross-model-{sonnet,luna}/`,
  which is gitignored. Captures are in `capture/llm_calls.jsonl`.
  - Restarting for phase 2 overwrote `web.log`. The phase-1 planner-attempt
    lines survive in `S4/turns-phase1.json`.
- **Driver.** `S4/run.sh` is `run_scenarios.sh` with `--out` pointed at
  `S4/sessions/<model>/`, so the audit's shared runs, digest and register were
  not touched.
  - One driver ran per instance, and each scenario was run once (n=1).
  - Review cards and proposals were auto-accepted, as in the audit.
- **Instruments.**
  - `turn_table.py` per model: `S4/turn-table-{sonnet,luna}.{md,json}`.
  - `S4/analyze.py` gives a per-turn table with OpenRouter's own
    `usage.cost`: `S4/turns.json` and `S4/turns-table.txt`.
  - `S4/guard_probe.py` runs the production `require_no_credential_material`
    over each provider-authored field of each captured call.
    - Positive control: on the audit's DeepSeek capture it finds the 4 known
      s06 reasoning hits.
    - Negative control: `Hello world` does not hit; `password: hunter2` does
      (`S4/guard_label_probe.txt`).
- **Spend**, from captured per-call cost (`S4/cost.txt`, `S4/cost-before-resume.txt`,
  `S4/cost-after-resume.txt`):

  | | Sonnet arm | Luna arm |
  |---|---|---|
  | Composer | $3.29 | $0.29 |
  | Advisor (GLM) | $1.35 | $1.64 |
  | **Arm total** | **$4.64** | **$1.93** |

  - Phase 1 cost $1.97 and phase 2 cost $4.59, against the $7 resumption cap;
    $6.57 in all.
  - No 402 or 429 errors occurred in phase 2.

## 3. Results

### 3.1 Boot (EXECUTED, `web.log` at each start; both starts gave identical lines)

| Boot line | DeepSeek (audit `tb1`) | Sonnet 5.5 | GPT-5.6 Luna |
|---|---|---|---|
| Composer "not reasoning-capable per litellm's registry" warning | yes | **no** | **no** |
| Advisor (GLM) not-reasoning-capable warning | yes | yes | yes |
| `composer_tool_contract_resolved`, planner | `openai_strict` | **`none`** | `openai_strict` |
| Loop strict count | 35 of 45 | **0 of 45** | 35 of 45 |
| Hatch (GLM) | `openai_strict` | `openai_strict` | `openai_strict` |
| Boot probe | skipped (debug mode) | skipped; `strict_key_omitted=45/23` | skipped |
| `composer_turn_budget_underfunded` (34 configured / 16 fundable) | yes | yes | yes |

Sonnet gets no strict tools. That is by design: `strict_transport.py` row 2
(D8) says "cache markers and strict never share a route". No boot line says
whether cache markers are on; only the captured messages show it.

### 3.2 Transport behaviour (EXECUTED from captures)

**Strict schemas.** No schema rejections on any route.

- Luna: all 67 composer calls were accepted, on the strict planner palette
  and the strict loop.
- The strict GLM escape hatch (the F19 path, the full 23-tool palette) ran
  once, on Luna r01 T1 (seq 13). It took 36.7 s, and its proposal was accepted.

**Thinking replay, Sonnet.**

- OpenRouter returns reasoning as `provider_specific_fields.reasoning_details`
  with an opaque `signature`. LiteLLM gives no `thinking_blocks`.
- So the composer replays assistant tool-call turns without thinking
  (`provider_gateway.py:523` replays only `thinking_blocks`).
- Anthropic, via OpenRouter, accepted every such request: all 32 tool-loop
  calls across s01, r01 and s06 T2, with reasoning on. No replay refusal was
  seen.
- The D1 rule about replayed `thinking_blocks` therefore never applies on
  this route.

**Prompt cache, Sonnet** (explicit markers):

- **Tool loop:** 92–97% from the second loop turn on (s01 T2–T8). The
  catalog and history-tail markers work.
- **Cold loop turn:** the first loop turn of a cold cache pays a large write.
  r01 T2 hit 63% and cost $0.47 for the composer.
- **Planner:** stuck at about 74%. The planner's second message, the
  57,476-char `{"authoring_aids":{"discovery_digest":…}}` block, carries no
  marker.
  - It does not start with `CATALOG_CONTEXT_PREFIX`, so
    `apply_anthropic_cache_markers` (`llm_response_parsing.py:943-958`, called
    at `pipeline_planner.py:3600`) skips it.
  - It is byte-identical across all phase-1 Sonnet planner calls (sha256 prefix
    `05911576`, computed from the captured requests).
  - So about 20k tokens are re-read uncached on every planner call. That is
    about $0.040 of the ~$0.050 cost of a Q&A planner call.

**Prompt cache, Luna** (automatic): 75% on planner Q&A, 85–94% in the loop.

**Advisor cost is now the larger driver for both cheap composers.** GLM
advisor cost per tool-loop turn grows with the session:

| Session (s01, T2 → T8) | Advisor cost per turn |
|---|---|
| DeepSeek (audit) | $0.04 → $0.10 |
| Luna | $0.05 → $0.16 |
| Sonnet | $0.09 → $0.25 |

The advisor's cache hit was 48% on the Luna arm and 64% on the Sonnet arm.
GLM also wrote one verdict's findings in Chinese (Sonnet r01 T2, seq 22).

**Empty response, Sonnet.** One call (p01 T1, effort `low`) returned empty
content with `finish_reason=tool_calls` and no tool calls. The planner
recovered with `prose_nudged`. This is 1 of 69 Sonnet composer calls.

**Credit sensitivity.** The tool loop reserves `max_tokens=65536`, about
$0.66 of headroom per Sonnet call. With a low balance those calls fail with
402 while the cheaper calls still pass. The user sees `502 llm_unavailable`
(phase 1).

### 3.3 Per-turn measurements (EXECUTED; raw: `S4/turns-table.txt`)

Latency percentiles are nearest-rank over completed, uncontaminated turns.
Composer cost excludes the advisor.

| Measure | DeepSeek (audit, same turns) | Sonnet 5.5 | Luna |
|---|---|---|---|
| Q&A planner turns (p01, s04), latency p50 | 8.3 s (n=10) | 6.4 s (n=9) | 7.3 s (n=10) |
| Q&A planner composer $ per turn | $0.001–0.003 | $0.049–0.054 | $0.004 |
| Edit-loop turns (s01 T2–8, r01 T2–3), p50 / max | 16.3 s / 28.5 s (n=9) | 20.8 s / 48.3 s (n=9) | 15.6 s / 59.7 s (n=8) |
| Edit-loop composer $ per turn, median (range) | $0.006 ($0.003–0.016) | $0.071 ($0.031–0.469) | $0.010 ($0.006–0.036) |
| Edit-loop advisor $ per turn, median | $0.07 | $0.10 | $0.14 |
| Proposal turns: first `emit_pipeline_proposal` accepted | 27 of 40 (battery) | **4 of 4** (s06, t01, r01, s01) | 1 of 4 (s01) |
| Luna rejection codes | | | t01 `locked_input_extras`; r01 `locked_input_extras`, `canonical_schema` ×2, then hatch |
| s01 edits: valid graph after every turn | 8 of 8 | 8 of 8 | 8 of 8 |
| Provider/transport errors | none | none (402s in phase 1 only) | none |

### 3.4 Finding × model

Paths are relative to `S4/sessions/<model>/<scenario>/turnNN/`.

| Finding | DeepSeek v4.1 Flash | Sonnet 5.5 | GPT-5.6 Luna | Model-independent? |
|---|---|---|---|---|
| **F1** two files on a new session | REPRODUCES: 240 s timeout, then output cap | **REPRODUCES** (root cause), shown differently. In s10 T1 and T2 the same rejections occurred (`source_path_policy_invalid`, then `plugin_options_invalid` when it tried `blob_id` in named `sources`). Sonnet then wrote a correct, specific "can't bind two uploads to named sources" reply in 51 s, with no hatch and no timeout. The advisor FLAGGED it as an unproven blocker, so both turns were **WITHHELD** | **NOT REACHED.** Luna never listed the blobs and asked the user to upload files that were already attached. The advisor admitted it. See the note below the table | **Yes.** The schema/custody gap reproduces on Sonnet. The model sets only how the user sees it fail: a timeout, a withheld reply, or a wrong question |
| **F2** advisor withholds correct "not available" answers | 4 of 6 withheld | **0 of 3** (s04 T2, p01 T2, p01 T3 admitted). Same mechanism on a build blocker: **2 of 2** s10 blocker replies withheld | **2 of 2** (s04 T2, p01 T3) | **Yes.** The advisor cannot verify denials or blockers it has no evidence for. The rate depends on phrasing |
| **F2, converse** | not seen | not seen | **REPRODUCES:** p01 T2 "Yes. I can read PDFs when you upload one…" (false) was admitted CLEAN | **Yes** |
| **F3** `diff_pipeline` has no baseline | 4 of 4 | **REPRODUCES:** s01 T6, r01 T2 | **REPRODUCES:** s01 T6, r01 T2 (`diff_baseline_unavailable`) | **Yes** |
| **F4** credential guard fails the turn: reasoning | 2 of 2 (s06) | DOES NOT: 0 hits in 69 calls; signatures use standard base64 | **REPRODUCES**, different trigger: r01 T3 "Run it." gave 422. Luna's `reasoning.encrypted` blob (base64url) contained `sk-9vwYX03zKEq9…`, which matches the `openai_key` pattern. 1 of 37 encrypted blobs | **Yes.** Opaque provider blobs are scanned as if they were text |
| **F4b** credential guard: visible content (new) | not seen | **REPRODUCES:** s04 T5 "Is this safe to run on sensitive data?" gave 422 (`**Credentials:**` label). **Not again** in 9 edit-loop turns or 27 turns overall | DOES NOT | **Yes**, the detector itself. **D6 is approved; CDA-19 is fix-scheduled** |
| **F5** model by slug / unfiltered `list_models` | 3 of 3 misleading | **REPRODUCES:** p01 T1 offers a catalog of 3,972 models and says the profile's model "isn't exposed". r01 T3: "this session doesn't show me which concrete model that is" | **REPRODUCES:** p01 T1 lists the litellm provider dump | **Yes** |
| **F8** receipt on every tool-loop reply | 29 of 29 | **REPRODUCES**, 9 of 9 completed loop turns | **REPRODUCES**, 8 of 8 clean loop turns. Advisor-down withhold (s06 T2): the saved-scope sentence appears twice | **Yes** |
| **F9** "Correction to my previous message" | 2 seen | **DOES NOT**, 0 of 4 FLAGGED→repaired replies (s01 T2, T3, T4; r01 T2) | **DOES NOT**, 0 of 1 (s01 T3) | Phrasing is model-dependent. The cause, the repair-instruction wording, is not; keep R9 |
| **F10** "emit now" notice | 29 of 41 | **REPRODUCES** on 7 planner turns, including informational p01 T1 and 3 copies in s10 | **REPRODUCES** on 5 planner turns, including p01 T1 | **Yes** |
| **F17** internals / "file a bug" | 0 in the battery | DOES NOT (0 of 22 completed replies) | DOES NOT (0 of 22) | No evidence |
| **F18** tutorial precondition | 3 of 3 | **REPRODUCES:** t01 has the "Consider adding error routing…" suggestion, 1 `json` output, all `discard` | **REPRODUCES:** t01, same shape | **Yes** |
| Planner empty first reply | not seen | 1 of 69 calls | not seen | Model quirk (handled) |
| Unrequested edit on a question (new, n=1) | not seen | r01 T2 "What changed…?": the advisor FLAGGED the read-only answer for a missing prompt-injection shield. Sonnet then edited the pipeline (`patch_node_options`, `request_interpretation_review`), disclosed the edit and staged a card | not seen | Advisor-driven, not composer-driven. Relevant to the CDA-07 rubric |

**A gap in the harness, not yet a product claim.** In s10 and r01 the
planner's request carries the user's text and an `information_manifest`
(`discoverable_classes: … blob.metadata …`), but no list of the files
attached to the session (`turn01/llm_calls.json`, message 2).

- `drive_session.py` uploads attachments as session blobs before turn 1 and
  never references them in the message.
- Sonnet and DeepSeek found the files with `list_blobs`. Luna did so in r01
  ("Here's our feedback export") but not in s10 ("these two quarters").
- I did not check whether the real frontend sends attachment references with
  the message. If it does not, the empty-session planner is never told that
  files are attached. Recommend that CDA-10 or CDA-13 verify this first.

### 3.5 Credential-guard defects in detail (EXECUTED)

**F4b, visible prose.** The rejected Sonnet reply contained
`- **Credentials:** Credentials and model bindings are managed by your operator.`
Minimising it with the production detector (`S4/guard_label_probe.txt`):

- `- **Credentials:**`, `Credentials: managed by your operator.`,
  `**Password:** never shown` and `credentials: **` all HIT
  `credential_assignment`.
- `**Secrets:**`, `**API keys:**`, `## Credentials:` and `password:` do not.
- Controls behave: `password: hunter2` hits and `Hello world` does not.
- Cause: the pattern (`contracts/credential_material.py:85`) accepts any run
  of non-space characters as the value, including `**` and plain words.
- It is shared with `web/validation.py:180`.
- Across the 180 captured calls, the guard probe found 2 hits: this one, and
  the Luna one below.

**F4, opaque blob (new trigger).**

- Luna returns `reasoning_details` items of type `reasoning.encrypted`
  (`format: openai-responses-v1`). Each `data` field is about 3,000 chars of
  base64url.
- base64url contains `-`, so the `openai_key` pattern (`sk-…`) can match by
  chance. It did on 1 of 37 blobs. At about 3 calls per turn that is several
  percent of Luna turns.
- The turn failed with 422 on surface `composer_provider_response`. The model
  had done nothing wrong; its tool call was a correct `request_advisor_hint`.
- Anthropic signatures use standard base64, which has no `-`, so `sk-` cannot
  occur in them.
- D1/CDA-18 already covers this, because the hit is only in
  `reasoning_details`. The better rule is to never pattern-scan opaque
  encrypted or signature fields at all.

## 4. Recommendation for the lanes

**Treat as model-independent** (fix whatever the demo model): F1, F2 (both
directions, and on build blockers), F3, F4 (opaque blobs), F4b, F5, F8, F10 and
F18.

**Model-dependent on this evidence:**

- F4's DeepSeek trigger (sample secrets drafted in reasoning);
- the F9 opening phrase;
- the F2 withhold rate;
- Luna not looking for attached files;
- Sonnet's rare empty response.

Proposed edits to `work-packages.md` (stated, not made):

1. **CDA-07 acceptance.**
   - Replace "A planted false capability claim is withheld." with: "A planted
     false capability claim is withheld. S4's Luna p01 T2 ('Yes. I can read
     PDFs…') is FLAGGED. S4's Sonnet s10 blocker reply ('can't bind two
     uploads to named sources') is admitted once the record carries the
     schema fact."
   - Add: "Measure on at least two composer models."
   - Add a rubric sentence: on a read-only question, the advisor must not
     demand mutations (S4 Sonnet r01 T2).
2. **CDA-18.** Add: "`reasoning_details` items of type `reasoning.encrypted`
   and signature fields are opaque. Never pattern-scan them; drop or keep them
   by provider contract."
   - Test fixture: the Luna blob shape (`data` holding `sk-` + 20 or more
     base64url characters). It must not fail the turn.
3. **CDA-19 (D6 approved, fix-scheduled).** Narrow `credential_assignment` so
   that markdown or punctuation-only values and prose continuations do not
   match.
   - Fixtures: the rows of §3.5. `password: hunter2` must still hit.
   - Order: after CDA-18 (`test_credential_guard.py`).
   - Sonnet's edit loop showed no further F4b trips (0 of 9), so the known
     exposure is answer-style replies.
4. **CDA-08.** "The receipt is never duplicated, including on the
   advisor-unavailable withhold" (S4 Luna s06 T2).
5. **CDA-16.**
   - Mark the planner's byte-stable `authoring_aids` message for Anthropic
     caching, in `apply_anthropic_cache_markers`; 2 of 4 marker slots are in
     use. Expected effect: a Q&A planner call drops from about $0.050 to
     about $0.011 (INFERRED from the measured token split; measure it).
   - Add the advisor's growth in cost per turn and its 48–64% cache hit as the
     larger driver, which is model-independent.
6. **CDA-10 / CDA-13.** First verify whether the empty-session planner is told
   which files are attached (§3.4 note). R4(b)'s "name the problem and hand
   off" must also survive the advisor. On Sonnet s10, a correct hand-off was
   withheld (couples to CDA-07).
7. **§6 re-run.** Use two models. Keep the account balance well above
   $1 per concurrent Sonnet loop call.

**Demo composer.**

- **Luna: no.**
  - Its first proposal was accepted on only 1 of 4 builds; r01 T1 took 98.9 s
    and needed the hatch.
  - It made a false PDF capability claim that the advisor admitted.
  - It ignored the attached files in s10.
  - It failed a turn on the opaque-blob guard.
- **Sonnet is the strongest composer measured**, but switching is not
  justified before CDA-19 and CDA-07 land.
  - **For:**
    - its first proposal was accepted on 4 of 4 builds;
    - 8 of 8 s01 edits were valid, with honest disclosures: the JSON-in-`.csv`
      warning, "I followed your request literally";
    - s10 failed fast (51 s) with a correct explanation, instead of timing out;
    - Q&A p50 was 6.4 s.
  - **Against:**
    - edit turns are slower: p50 20.8 s against DeepSeek's 16.3 s on the same
      turns;
    - the "is this safe?" turn failed on F4b;
    - its correct s10 explanation was withheld;
    - composer cost is about 12× DeepSeek per edit turn ($0.071 against
      $0.006) and about 25× per Q&A turn. Including the advisor, a Sonnet edit
      turn is about $0.17 against about $0.08.
  - For a demo of 15–20 turns, the Sonnet premium is roughly $1–2, which is
    not a reason against it. The open defects are.

## 5. Open questions

What would change this recommendation:

- **Sample size.** n=1 per turn. The F2 rates, F4b and the opaque-blob trip
  are single observations. The detector behaviour itself is deterministic
  (§3.5).
  - Repeat p01, s04 and s10 ×3 on Sonnet after CDA-07 and CDA-19.
- **Attachments.** Whether the production frontend tells the planner which
  files are attached (§3.4).
- **Thinking replay** on a direct-Anthropic or Bedrock route, which fills
  `thinking_blocks`. It is not measured; that route exercises replay and the D1
  carve-out.
- **Resume commands** (main checkout; check
  `curl -s https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OPENROUTER_API_KEY"` first):

  ```bash
  S4=.claude/lanes/composer-desk-audit-20261112/spikes/S4-cross-model
  $S4/start_instance.sh 8521 S4-cross-model-sonnet anthropic/claude-sonnet-5.5
  $S4/start_instance.sh 8522 S4-cross-model-luna openai/gpt-5.6-luna
  $S4/run.sh 8521 S4-cross-model-sonnet sonnet <scenario> ... > $S4/run3-sonnet.out 2>&1 &
  # stop each launch_web.py PID printed by start_instance.sh afterwards
  ```
