# Acceptance battery round 3 — report

Date: 2026-08-05. Driver: `claude-r3-deploy` (main session, sole AWS mutation
custodian). Instance: `https://elspeth.aws.foundryside.dev`.
Deployed release: `release/0.7.2@90d5508fd` as `a-fa1b99c60192978b10f7-web:10`,
image `RC-050826` (`sha256:5cc66a0e83f4…`). Session store recreated at
`SESSION_SCHEMA_EPOCH` 45; Landscape untouched at epoch 30.

Predecessor: `2026-08-04-battery-round-2-report.md`. Scope of record:
`round3-scope-reconciled.md` (which supersedes the round-3 brief's scope
section). Graph corpus and intents: `round3-graph-corpus.md`.

## Headline

The redeploy discharged its purpose: **ADR-036's authoring projection and its
runtime binding are both confirmed live.** Its custody NFR is *not* — that
assertion is over Landscape call records, and the web-API search I first
offered as evidence turned out to be non-probative (see the addendum). So the
bug class is proven inexpressible and the binding proven correct; what remains
unverified is that no persisted call record carries the bucket literal.

The round's substantive finding is that the **compose-vs-runtime schema seam is
wider than the fix wave addressed**. Four restamp fixes landed on specific
transforms' declared *output* contracts; round 3 found **three further
independent instances** of the same shape — compose succeeds, `/validate`
returns `is_valid=true`, and the run fails on a schema contract the engine
enforces but the composer did not model. They are unrelated to each other and
to the four that were fixed.

A second, orthogonal finding is that when such a run fails, the operator often
cannot see why: one P1 blinds the run API entirely, and a P2 leaves the run
accounting internally contradictory.

## Per-graph results

| Graph | Run | Terminal state | Verdict |
|---|---|---|---|
| g01 linear multi-transform | `da440bae-d960-4af3-a913-0603bbb2afc7` | `completed` (serial re-drive) | **INTEGRITY PASS**; the 422 was load |
| g02 gate + named routing + poisoned row | `bb683d20-bf9a-4c47-bc91-d9b0bb7bbcbf` | `completed_with_failures` 4/1/1 | **INTEGRITY PASS** — routing destinations unverified |
| g03 fork / coalesce | `b944ac9c-52a1-421b-8ed2-93b8b982cfdf` | `completed` | **INTEGRITY PASS** — no token accounting captured |
| g04 `json` source + explode | `57cc5039-0567-4edf-a4df-dee0c912c15e` | `completed` 6/0/0 | **INTEGRITY PASS** |
| g05 `text` source + `text` sink | `adf0b6c6-bdcb-4e29-ba23-b953bae5366c` | `failed` | **NEW** `elspeth-cfcd333f83` |
| g06 sink variety | `5c9964cb-974c-4f24-9198-45e087d4e47b` | `completed` | **INTEGRITY PASS** — no token accounting captured |
| g07 profile-first Textract | `67d40936-2261-4b30-9b43-7058f7927e53` | `completed_with_failures` 1/1 | **PASS** — ADR-036 authoring + binding; custody NFR unverified |
| g07 (first attempt) | `c69b6ab6-e462-46d8-bd9b-2c8a811bb02f` | failed at **source** | **NEW P1** `elspeth-47fa7c01eb` |
| g08 two-LLM-arm `row_union` A/B | `dc689cfe-9f4b-47ba-bd04-abd04309debc` | `failed` | Compose fix holds; **NEW** `elspeth-902fc354b2` |
| g09 four LLM nodes | `307c4e5c-0e5b-48b2-a64a-7142431e2c77` | `completed` 15/18, closure closed (serial re-drive) | **INTEGRITY PASS**; no cap wedge |
| g10 LLM → fixed-schema mapper | `37caf1ee-00c2-48af-b3ca-2f5d39963154` | `completed` | **PASS** — `elspeth-ed2c2315d7` confirmed |
| g11 `llm` source, dynamic schema | `a4f534df-2319-46be-95e0-453028375c13` | `failed` | Parity gap closed; **NEW P1** `elspeth-39118dd24f` |

## Fixes confirmed live

- **`elspeth-cd0f6a6cd9` / ADR-036 — both halves.** *Authoring:* given prose
  naming no bucket, region or prefix, the Composer authored
  `profile: acceptance-docs`, `key_field: document_path`, and no location
  vocabulary at all — decision 3's allowlist projection holding on a real
  graph. *Runtime:* **the real document was successfully analysed.** That is
  the proof: a success requires the correct bucket, a matching region, and a
  readable object, so it certifies the binding positively. It is also
  validator-guaranteed rather than transcribed — run `67d40936` returned
  **HTTP 200** for `completed_with_failures`, and `schemas.py:491-492` refuses
  to project that status unless `tokens.succeeded > 0`, so `succeeded >= 1` is
  enforced by the endpoint that served the response.

  The deliberately-absent key failed (`reason: submit_failed`,
  `error_type: service_error`) and took the `on_error` route. **Corrected:**
  an earlier draft rested the claim on the *absence* of
  `bucket_region_unverified`. That negative excludes only alias-as-literal, not
  mis-binding in general — a profile bound to a real but wrong bucket passes
  HeadBucket and the region check, then fails `StartDocumentAnalysis` on the
  same `service_error` path (`textract_document_analysis.py:832-837`).
  Post-ADR-036 the negative is also guaranteed by construction, and an outcome
  that cannot occur is not a discriminating observation. Treat it as
  corroborating colour, not proof.
- **Custody NFR — NOT verified live.** The ADR's assertion is over Landscape
  **call records**, which could not be queried remotely. The web-API search I
  ran is **not a substitute and is non-probative**: `RunDiagnosticsResponse`
  does not project call records at all (`schemas.py:965`, with `:955` noting
  the payload lives in the audit DB under `calls.response_ref`), so those
  projections structurally cannot carry the literal. It would also have passed
  *before* ADR-036 — in the round-2 pre-state the composer wrote the alias
  `acceptance-docs` as the bucket value, so the real literal was absent from
  row data then too. A search returning zero in both worlds distinguishes
  nothing.
- **`elspeth-ed2c2315d7`** — g10 `completed`. This exact shape failed at
  runtime in round 2.
- **`elspeth-5a372d3267`** — g11's `/validate` returned `is_valid=true`. The
  round-2 defect was compose presenting a graph that then failed execution
  `graph_structure` validation; that parity gap is closed. (The run failed for
  an unrelated reason — see `elspeth-39118dd24f`.)
- **Operator grant** — see below.

Two entries were **withdrawn from this section** after an adversarial check:

- **`elspeth-9c01c943a5` — NOT confirmed; 1 of 3 required samples clean.**
  This round's own corpus records the defect as intermittent at 1-in-2 and
  mandates three samples in three fresh sessions. One was taken, because
  concurrency 422'd the others. Round 2 saw the raw 500 on 1 of 2 attempts, so
  a single clean pass is fully consistent with the defect still being present.
  Stochastic items never close on one pass. Remedy is cheap — re-drive
  serially ×3; see the addendum.
- **`elspeth-03f5728c33` — retracted, then CONFIRMED on the missing read.**
  My original evidence (an empty `?status=pending` listing alongside a
  `/validate` that named reviews) was non-discriminating: that is *also* the
  ticket's defect signature verbatim, and the driver reads the listing strictly
  **before** `/validate` and never re-reads. Retracted on those grounds. The
  g08 sampling then stranded a fresh session (`6fc8b619-3602-4510-90c1-8470f385a750`,
  compose 422 at 271s) and I took the read that was missing:

  | | `?status=pending` |
  |---|---|
  | before `/validate` | **0 events** |
  | after `/validate` | **7 events**, all user-actionable |

  The seven carry real terms — `llm_prompt_template:llm_sentence`,
  `inline_source_data`, `required_control_auto_wired` ×2, `drop_raw_html_fields`
  — across `invented_source`, `llm_prompt_template` and `pipeline_decision`.
  That is the fix: `/validate` runs the surfacer in repair mode before
  delegating, so the listing is populated by it rather than left empty.
  Recorded on the ticket as comment 2345.
- **Operator grant** — `transform:aws_textract_document_analysis` appears in
  `/api/catalog/policy` `available_plugin_ids`. Per
  `test_textract_without_a_profile_table_is_hidden_even_in_a_supported_region`,
  an empty grant table hides the plugin with `PROFILE_UNAVAILABLE`, so its
  presence is positive proof the grant is live rather than merely configured.

## Not confirmed

- **`elspeth-9d13900064`** — **reproduced on the deployed fix.** It is at
  `verifying`, so this round *was* its live acceptance and it failed. The
  root cause it names is genuinely cured and its regression test passes; the
  symptom recurred through a different path (`elspeth-902fc354b2`).
  **Recommend it stays at `verifying`.** Negative datum recorded as comment
  2340.
- **`elspeth-558fa5a321`** — not exercised. The ≥4-auto-wired-control shape
  needs four LLM nodes, and that compose did not converge (below).
- **`elspeth-49b467d91a`** — not sampleable through the API at all. The fix
  restores frontend input state; the observable is browser DOM. Needs
  Playwright, not the driver.
- **`elspeth-454892147c`** — still no fault-injection lever on the acceptance
  surface. Unsampled, as in round 2.
- **`elspeth-2306940c70`** — claimed by another agent mid-round; not driven.

## New defects — all labelled `battery-2026-08-05`

| Id | P | Summary |
|---|---|---|
| `elspeth-47fa7c01eb` | 1 | A run with `tokens.succeeded == 0` 500s `/runs/{id}`, `/diagnostics`, `/outputs`, `/results` **and** `/api/sessions/{id}/runs` — one bad run makes every other run in the session unenumerable |
| `elspeth-39118dd24f` | 1 | `llm` transform demands its own declared output field as a required *input*; fails every row of any fixed/flexible-schema llm node |
| `elspeth-902fc354b2` | 2 | Locked-input extras check is bypassed by any resolvable node between `row_union` and the consumer, and is blind to pass-through fields |
| `elspeth-cfcd333f83` | 2 | `llm` output-field collision with an existing row field caught only at runtime |
| `elspeth-82d4c5146c` | 2 | Input-validation `PluginContractViolation` records no terminal outcome — token left pending, run integrity `open` |

`elspeth-47fa7c01eb` is the one to fix first, and not only for its severity:
it is what stopped this round from diagnosing its own first Textract failure
from the API. The real failed node was recoverable only from CloudWatch.

## Methodology findings

**Compose wall-clock is the round's dominant operational constraint, and it is
variance, not load.** This finding was revised twice; the final reading rests
on the full sample.

I first attributed three `convergence_wall_clock_timeout` 422s to my own
six-way concurrency, on the strength of one control (g01: 422 at 271s under
load, **197s / HTTP 200** alone). That correction was itself wrong. Re-driving
g08 **serially** produced a 422 at 271s — for a graph that had composed
successfully at 238s *under* the same load I blamed.

The full distribution over 15 composes:

- succeeded (12): 43, 61, 69, 75, 95, 96, 119, 128, 133, 151, 197, 238 s
- hit the wall (3): 271, 271, 272 s

The same graph swings enormously: g09 ran 96s and 272s; g01 197s and 271s; g08
238s and 271s. There is no clear relation to graph complexity either — g02
(gate, named routing, poisoned row) composed in 43s while g01 (linear
multi-transform) took 197s.

**Conclusion: compose duration is highly variable across the whole 43–272s
range, and the 270s budget truncates the tail. Roughly 1 in 5 composes dies at
the wall.** Concurrency plausibly adds some load but does not determine the
outcome, and no defect was filed for it. A per-task reasoning-budget change was
landing as this round ran; it targets exactly this variance, and round 4 should
re-measure rather than treat these numbers as a baseline.

Operationally the tail is not wasted: a 422 leaves a genuinely stranded
session, and that is how `elspeth-03f5728c33` got its live confirmation.

**The engine's error text can point the wrong way.** `elspeth-39118dd24f`
reports "This indicates an upstream transform/source schema bug" for a row
whose upstream behaved correctly. Two of this round's five findings needed the
composed graph JSON, not the error message, to locate the real cause.

## Compose-loop efficiency profile

Measured from the authoritative record — `chat_messages` via
`GET /api/sessions/{id}/messages`, corroborated by a read-only query against
the session store (`ops-local/acceptance/{analyse_compose,make_inspect_override}.py`).
17 sessions, 162 assistant turns, **193 tool calls**.

**The loop is not bloated.** 6–12 assistant turns per compose at ~1.0–1.5 tool
calls per turn. There is no call explosion and no runaway turn count.

**Model reasoning is persisted — but not in the session store.** An earlier
draft of this section claimed it was not persisted at all; that was wrong, and
wrong in a way that mattered, because it implied the reasoning-effort change
could not be measured before/after. The correction:

- `chat_messages` has no reasoning column (`id, session_id, role, content,
  raw_content, tool_calls, tool_call_id, sequence_no, writer_principal,
  created_at, composition_state_id, parent_assistant_id`), and `raw_content`
  is populated on only 60 of 542 rows (assistant only, 63–8462 chars, mean
  ~1000). That is what my session-store query saw.
- Reasoning lives on the **`ComposerLLMCall` record** instead:
  `reasoning_content: str | None` (`contracts/composer_llm_audit.py:167`),
  parsed from the provider response (`llm_response_parsing.py:625`), alongside
  `reasoning_details` and `thinking_blocks`. `guided_audit.py:207-209` nulls
  those three **only when `call.status is not SUCCESS`** — successful calls
  retain them.
- Those records go to the audit trail (Landscape), which is the acceptance
  RDS and was not queryable from this session. So reasoning volume *is*
  measurable; it just needs a Landscape-side query, not a session-store one.

**Implication for the reasoning-effort change** (`elspeth-dc459d438e`, merged
after this round's deployed build): a before/after on reasoning volume is
available from `ComposerLLMCall.reasoning_content` lengths. The new settings
default to `discovery=low`, `candidate=high`, `advisor=medium` and all carry
defaults, so no task-definition change is required — but they are new
`ELSPETH_WEB__` names, so if any are set explicitly they must land in the same
task-definition revision as the image that knows them (the `config.py:1134`
trap that gated the Textract grant).

**Tool histogram across every session:**

| Tool | Calls | | Tool | Calls |
|---|---:|---|---|---:|
| `get_plugin_schema` | 60 | | `get_blob_content` | 4 |
| `set_pipeline` | 52 | | `preview_pipeline` | 4 |
| `patch_node_options` | 23 | | `get_plugin_assistance` | 4 |
| `request_interpretation_review` | 17 | | `patch_output_options` | 3 |
| `get_pipeline_state` | 14 | | `upsert_node` | 3 |

`get_plugin_schema` + `set_pipeline` are **58% of all tool calls** — 3.5 schema
lookups per compose.

**Identical repeated calls track compose time almost monotonically.** Grouping
on `(session, tool, byte-identical arguments)`:

| Session | Compose | Identical repeats |
|---|---|---|
| g02 | 43s | 0 |
| g10 | 69s | 0 |
| g08-s2 | 106s | 1 |
| g08-s3 | 151s | 1 |
| g01 | 197s | 2 (`set_pipeline` ×3) |
| g08-s1 | **271s → 422** | 3 (`set_pipeline` ×4) |

Store-wide, `set_pipeline` is repeated byte-identically in six sessions
(×3 twice, ×2 four times) and `patch_node_options` in four. The slow session's
turn trace is the shape: `set_pipeline` at seq 7, 11, 13, 15, 18 with a single
`get_pipeline_state` between, each rendering the same user-visible line —
*"ELSPETH is applying a pipeline correction."* The model is re-issuing a
rejected mutation unchanged rather than converging.

Note this metric only catches **byte-identical** arguments, so it is a floor:
near-identical repair retries are not counted.

**Two ambient costs worth naming:**

- **The advisor early checkpoint flagged 14 of 16 composes**
  (`composer.advisor_checkpoint_pass ... phase=early verdict=flagged`). Whatever
  its per-case merit, a ~88% flag rate is a near-constant repair tax rather
  than an exception path.
- **97 LiteLLM warnings** of *"Potential consecutive user/tool blocks. Trying
  to merge"* on Bedrock calls, i.e. the conversation handed to the provider
  repeatedly needs reshaping before it can be sent.

**Reading:** the win is not in trimming turns or tools — it is in (a) not
re-issuing rejected mutations unchanged, and (b) the advisor flag rate. A
per-task reasoning budget should also compress the 43–272s spread directly,
since turn *count* barely moves while wall-clock varies six-fold. Measure it from `ComposerLLMCall.reasoning_content` in Landscape.

## Two things this round learned

1. **Re-drive before concluding.** The first g07 attempt looked like a Textract
   failure. It was not — it died at its source node and never reached the
   transform. Reasoning from `succeeded=0` would have produced a false
   ADR-036 reopen; re-driving produced the opposite conclusion, and the
   re-drive's failure carried a *different* error code than the defect
   signature, which is what makes it evidence.
2. **`verifying` is not `closed`, and a battery is not a formality.** The
   correct disposition for `elspeth-9d13900064` was neither "reopen" (nothing
   to reopen) nor "the fix works, file a sibling and move on". This round was
   its live acceptance; it failed; the ticket must not advance.

## Addendum — adversarial check of this report

An independent adversarial pass over the first published version
(`e77e199f3`) returned **2 upheld, 3 overclaimed, 1 cannot-determine**. Its
full working is in `round3-scope-reconciled.md`, section "Adversarial check of
the round-3 report". Everything above already incorporates the corrections;
they are listed here so the change is visible rather than silently folded in.

| Item | Verdict | Effect |
|---|---|---|
| ADR-036 both halves | OVERCLAIMED — conclusion survives, stated reason does not | Rewritten to lead with the successful analysis; the `bucket_region_unverified` negative demoted to corroboration |
| Custody NFR | OVERCLAIMED — test is non-probative | **Retracted.** Now recorded as not verified live |
| `elspeth-39118dd24f` (P1) | UPHELD | Discriminator verified by direct construction of the input model |
| `elspeth-47fa7c01eb` (P1) | UPHELD, and the open question ANSWERED | Reachable end-to-end; see below |
| `elspeth-9c01c943a5` | OVERCLAIMED — 1 of 3 mandated samples | Moved out of "confirmed"; re-driven ×3 serially |
| `elspeth-03f5728c33` | CANNOT DETERMINE | **Retracted.** Driver query order makes the evidence non-discriminating |

Three findings from that pass are worth carrying forward on their own merit:

1. **`elspeth-47fa7c01eb`'s reachability is settled: yes.** An all-quarantined
   run does yield `tokens.succeeded == 0` at the web layer, because quarantined
   tokens are recorded as `TerminalOutcome.FAILURE` with path
   `QUARANTINED_AT_SOURCE` and increment `failed_tokens`/`quarantined`, never
   `succeeded_tokens` (`web/execution/accounting.py:128-140`). The engine's
   contract carries an explicitly commented arm for it. The "web dropped an
   arm" framing is a mechanism, not a hypothesis. **But** the validator is not
   new — the same line is in round 2's deployed `173a81cbb`, so this is a
   long-standing divergence the round tripped, not a fix-wave regression.
2. **`elspeth-39118dd24f` narrows `elspeth-d1f20e8385`'s reachable surface.**
   That restamp fix only matters on the emit path, which is downstream of input
   validation — so for the fixed/flexible-with-response-field configuration it
   is unreachable in production until `39118dd24f` lands. The
   fixed-without-response-field case stays reachable, so the fix is not dead,
   but the two should be cross-linked.
3. **Integrity pass is not behavioural pass.** For `status == "completed"` the
   web validator already enforces closed accounting, `succeeded > 0` and
   `failed == 0`, so a terminal `completed` is a stronger claim than it looks
   and does foreclose the stranded-token shape. What it does **not** check is
   where rows went. Round 2 verified per-destination routing (g04: *"91/77 →
   `premium`, 44 → `standard`, `not-a-number` → `bad_rows`"*); round 3 did not.
   A named-routing regression sending every row to `standard` would still show
   `4/1/1` and still be marked PASS. The regression net has that hole; round 4
   should assert destinations, not just terminal state.

## Reporting posture

Recommendations only — **no ticket was closed by this round.** Operator
sign-off closes, and the close then carries
`close_commit release/0.7.2@<tip>`. AWS mutations are recorded in the ledger
in `2026-08-03-r3-rca-remediation-tracker.md`.
