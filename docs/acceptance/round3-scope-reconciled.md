# Round-3 live-verification scope — reconciled

Date written: 2026-08-05. Supersedes the **Scope** section of
`docs/acceptance/2026-08-05-battery-round-3-brief.md` (that brief listed four
confirm-items; fourteen fixes now ride round 3). The brief's *preconditions*,
*driver*, and *reporting* sections remain current and are not restated here.

Sources: the R3 tracker (`2026-08-03-r3-rca-remediation-tracker.md`, roll-up
lines 85–125 + checkpoint log), the round-2 report
(`2026-08-04-battery-round-2-report.md`), live Filigree, and the diff of every
named fix commit.

## Three facts that reshape the round

1. **Deployed `173a81cbb` already contains two of the "unsampled" fixes.**
   `git merge-base --is-ancestor` against the deployed tip: `937a8010b`
   (retry-exhaustion routing, `elspeth-454892147c`) and `c0c28b94f` /
   `a6cad7c63` (prompt retention, `elspeth-49b467d91a`) are **IN-DEPLOYED**.
   Round 2 did not fail to deploy them — it failed to *exercise* them. Every
   other fix below is **NEEDS-REDEPLOY**.
2. **The stranded session named as the repro for two P1s will not survive the
   preconditions.** `elspeth-558fa5a321` and `elspeth-03f5728c33` both name
   session `e445e8e2-9ccf-48a5-8fd8-10ffdc68b5f9` as the live un-wedge path
   ("no data surgery"). Brief precondition 3 recreates the session store at
   `SESSION_SCHEMA_EPOCH` 45, which **destroys that session**. Both recipes are
   void; round 3 must construct a fresh strand.
3. **`close_commit` is a release-tip stamp, not the fix.** The Fix commit(s)
   column below is sourced from `fix_verification` + the tracker checkpoint log,
   not from `close_commit`. E.g. `elspeth-9c01c943a5` carries
   `close_commit release/0.7.2@6610fa0d4` but was fixed by `cbc0e99d3`.

Release tip at writing: `release/0.7.2@90d5508fd`.

---

## A. Fixes landed since round 2

A1 and A2 need live confirmation. **A3 does not** — it is listed here so round 3
does not spend budget on it.

### A1 — landed since the deployed build (require the redeploy)

| Ticket | Priority | Filigree status | Fix commit(s) | What must be observably TRUE on the live instance | Concrete repro shape to drive it | Composer web API alone? |
|---|---|---|---|---|---|---|
| `elspeth-9c01c943a5` | P1 | `closed` | `cbc0e99d3` (close stamp `6610fa0d4`) | A `pipeline_decision` draft mismatch settles as a **coded `arg_error` envelope with no event row** — never a raw uncoded 500. The session-aware tool dispatch now captures plugin crashes like the sync path, so *no* compose failure escapes to Starlette raw. | Freeform compose of the round-2 **g02b** shape: inline `csv` source → **two parallel `llm` transform arms** (both emitting a `verdict` field) → `row_union` → `field_mapper` → sink. Intent: *"classify each row two different ways with two LLM steps, then merge both results into one table."* The two arms auto-splice disclosure cards sharing the constant `user_term`, which is what collided. **Sample ≥3 fresh sessions** — round 2 saw it 1-of-2. Any 500 without a coded envelope = reopen. | **yes** |
| `elspeth-558fa5a321` | P1 | `closed` | `7fcd9a8a1` + `6610fa0d4` | Interpretation rate caps now govern **only LLM-authored `vague_term` rows**; `backend_auto_surface` provenance is excluded from the counters. A correction that surfaces **≥4 `required_control_auto_wired` cards in one turn** surfaces *all* of them (zero `RATE_CAP_PER_TERM`, zero terminal `AUTO_INTERPRETED_NO_SURFACES`), and the session reaches a validatable state. | Compose a graph, then send a **compose correction** that adds **three `llm` transforms** (the g08 `extract_1/2/3` shape) so the required-control pass auto-wires ≥4 controls (`prompt_shield_auto_*` + `content_safety_auto_*`). Intent: *"add three separate LLM extraction steps to this pipeline."* Then assert: `GET /interpretations?status=pending` returns **every** card (non-zero, ≥4); resolve them all; `POST /validate` no longer reports `interpretation_review_pending`. | **yes** |
| `elspeth-03f5728c33` | P1 | `closed` | `3880cb814` (behaviour; `09a484dcf` is a companion test-expectation repair for a pre-existing stale prompt assertion, not part of the fix) | **Both** `/validate` arms (bare head *and* explicit `state_id` — the arm the web UI always uses) run the backend surfacer in repair mode over the state they validate *before* delegating. A compose that died after persisting a mutating turn can no longer block validation with an empty review list; the validation suggestion also names the residual remedy. | **Construct a fresh strand** — the named session `e445e8e2…` is destroyed by the epoch-45 recreation. Drive a compose that adds `llm` nodes and let the client read-timeout mid-loop (round 2's 240s deferred cancellation), so `llm_prompt_template` requirements persist with zero events. Then call `POST /validate` **directly, with no intervening compose message**, and confirm the PT cards now appear in `GET /interpretations?status=pending`. Re-calling `/validate` must be idempotent (no duplicate cards) and must not resurrect resolved ones. | **yes** |
| `elspeth-cd0f6a6cd9` | P2 | `closed` | `1efeae10d` (ADR-036 merge) | Two arms. **(a)** Authoring the profile alias as a literal `bucket`/`bucket_field` value draws `error_code=profile_alias_used_as_bucket` with the repair suggestion *"Select the alias with the 'profile' option; rows carry relative object keys in key_field"* — **not** a `bucket_region_unverified` quarantine. **(b)** A correctly profile-bound run persists `{profile, relative key}` only: the bucket literal and key prefix are absent from run diagnostics, call records, and the public discovery digest. | Re-run the round-2 **g03b** shape. Arm (a): repeat run `e41d0e6b`'s intent verbatim — *"build a Textract pipeline using the aws_s3 source with the acceptance-docs profile"* — and let the planner author the alias as a bucket. Arm (b): author `profile: acceptance-docs` + `key_field` carrying the relative key `docs/exec-summary.pdf` (already in place, no upload), then read `GET /api/runs/{id}/diagnostics` and grep the call records for the bucket literal. | **partial** — arm (a) is pure web API. Arm (b)'s *negative* custody claim needs the run/audit surface (`/diagnostics`, Landscape), and the whole ticket is **blocked** unless `ELSPETH_WEB__AWS_TEXTRACT_PROFILES` is registered **in the same task-definition revision** as the post-ADR-036 image — otherwise the transform is honestly `profile_unavailable` and nothing is confirmable. |
| `elspeth-9d13900064` | P2 | `verifying` | `3d77d15bd` | **The failure moves earlier, it does not disappear.** Verified by hunk read: `_producer_emit_set` (`web/composer/state.py:2769`) returns the transform's `_output_schema_config.guaranteed_fields`, and `get_llm_guaranteed_fields` (`plugins/transforms/llm/__init__.py:110`) puts `<response_field>_usage` / `<response_field>_model` **into** that set. So the new extras-polarity walker's arm-emit union *does* carry the provenance fields into Rule A. Falsifiable form: **compose/validate must reject with `error_code=locked_input_extras`, whose `extra_fields` names `verdict_usage` and `verdict_model`**, before any run starts. Rule B (`sink_locked_extras`) shares the walker. Also closes a latent `/validate` `KeyError` on direct-sink producers. | Same **g02b** graph as `9c01c943a5` above (`csv` → 2 × `llm` arms each emitting `verdict` → `row_union` → **fixed-mode** `field_mapper` whose `schema.fields` list only `verdict` → sink). A green compose that then executes cleanly is **not** a pass — it means a different fix than the ticket claims; record a datum on the ticket. The round-2 signature to be absent: runtime `PluginContractViolation` / `extra_forbidden` at `executor_post_process`. | **yes** |
| `elspeth-5a372d3267` | P2 | `verifying` | `83a53388a` + `6f7a52eaf` | Two independent arms. **(a) Engine:** queue nodes propagate arm guarantees (intersection when all arms participate, total abstention if any abstains), so an `llm` source → queue → consumer no longer reports `producer guarantees '(none - dynamic schema)'`. **(b) Composer:** the "ready for the required review" announcement is now preceded by an authoring-masked re-validation; when that pass is red the published message is **qualified with the real objection** instead of claiming ready. | Re-run the round-2 **g08** shape: `llm` **source** (not transform) → `field_mapper` requiring `llm_response`. Intent: *"generate rows with an LLM and map the response into a table."* Assert the compose surface either validates clean or names `graph_structure` in its own reply — the round-2 failure was compose saying ready and `POST /validate` then rejecting. | **yes** |
| `elspeth-ed2c2315d7` | P2 | `verifying` | `0e960ef0e` | `field_mapper` restamps declared output field contracts in its emit path (matching its six sibling transforms), so an llm-produced field carrying inferred `required=False` metadata through a mapper whose authored `schema.fields` declare it `required=True` no longer trips `SchemaConfigModeViolation` at `post_emission_check`. The restamp must not mask a genuinely absent declared field. | Re-run the round-2 **g10** shape: linear `csv` → `llm` transform adding a `cuisine` field → `field_mapper` (`select_fields`, keeping `dish` + `cuisine`) → `json` sink. Intent: *"label each dish with its cuisine using an LLM, then keep just the dish and cuisine columns."* The run must complete, not fail mid-run. | **yes** |
| `elspeth-e89e6bf47a` | P2 | `closed` | `fb6e11dd4` | A **rejected** `set_pipeline` failure envelope carries the rejection entry **only**. The unchanged pre-mutation state's `no_source_configured` / `no_sinks_configured` must no longer ride alongside the real error on the raw-result surfaces (freeform chat tool messages, composer MCP) that serialize `ToolResult.to_dict()` verbatim. Discovery tools and incremental mutations keep their existing disclosure behaviour. | On a **fresh session with no source and no sinks**, drive a full-replacement `set_pipeline` that will be rejected (policy violation or invalid candidate). Read the tool message in `GET /messages` and assert the validation list contains exactly the `rejected_mutation` entry — no phantom `no_source_configured` / `no_sinks_configured`. Contrast: an *incremental* mutation failure on the same session must still disclose the standing state's errors. | **yes** |
| `elspeth-308d1e0831` | P2 | `verifying` | `1041bd84e` | Freeform auto-titles pass a **fail-closed shape allowlist** (Unicode L/N + bounded punctuation, 2–8 words, 60 chars **reject-not-truncate**, edge-punctuation rule, credential-shape reject). A runaway naming completion yields the **minted default title**, never a truncated fragment of raw model output. The outbound naming call ships only a redacted, 800-char fenced excerpt. | Create a fresh freeform session whose **first message** invites a runaway completion (round 2's leak was a request that the naming model answered instead of naming — e.g. a long "write me a CSV classification pipeline in python" first message). Assert the session title in the session list / `GET /api/sessions` is a clean 2–8 word title or the minted default — never a `#`-heading, code fence, or `import` line. | **partial** — the title shape is pure web API; the `composer.auto_title.rejected` counter increment is CloudWatch (`ELSPETH/Operator` namespace), not an API response. |

### A2 — already present in deployed `173a81cbb`, still never exercised live

These need **no redeploy**. Round 2 reported them "unsampled" against a build
that already contained the fix, so a round-3 sample is a first exercise, not a
re-test.

| Ticket | Priority | Filigree status | Fix commit(s) | What must be observably TRUE on the live instance | Concrete repro shape to drive it | Composer web API alone? |
|---|---|---|---|---|---|---|
| `elspeth-454892147c` | P1 | `verifying` | `937a8010b` (**IN-DEPLOYED**) | Retry exhaustion routes through the transform's configured `on_error` contract: `token_outcomes` shows `(FAILURE, ON_ERROR_ROUTED)` with `sink_name` = the configured sink, plus `retry_exhausted` transform-error and DIVERT evidence — **not** `(FAILURE, UNROUTED)` with no quarantine write. Parity invariant: the same exception with `retry_manager=None` must produce an identical `(outcome, path, sink_name)` triple. | **Not constructible from the acceptance surface.** Its own repro is `examples/rate_limited_llm` (`on_error: quarantine`, `retry.max_attempts: 3`) driven against `chaosllm serve --preset=realistic`, sampled repeatedly (~1 in 16 runs exhausts). | **NO** — requires *fault injection*: an induced retryable provider failure inside a retry-enabled pipeline. No such lever exists on the deployed instance, and round 2 saw zero natural provider failures across 12 runs. Confirm via a **local CLI run** against chaosllm, or report unsampled again. **Do not fake it.** Adjacent-but-not-substitute evidence: `on_error` routing to named quarantine sinks was live in runs `43743e6c`, `ea9ec481`, `e41d0e6b`. |
| `elspeth-49b467d91a` | P1 | `verifying` | `404f571c6` → merged `c0c28b94f`, remediated `a6cad7c63` (**IN-DEPLOYED**) | On the **plain guided** surface, a failed send **restores the user's typed prompt** in the composer input (delivery-predicated retention: chat-history seq-floor verbatim match / cold-start durable checkpoint). A *delivered* send still clears it. The tutorial frame is deliberately excluded, and no prompt leaks across sessions. | Drive a plain guided send into a failure. The brief's cheap recipe: the **contradictory-instruction shape that FLAGs the advisor** reliably produces a failing compose — *"remove the gate entirely but keep the guarantee that only amounts>100 reach big_amounts"*. Then inspect the composer input, and separately confirm a successful send still clears it. | **NO** — the fix is frontend input-state restoration. "The typed prompt is restored in the input" is a **browser** observation (Playwright against the deployed surface, or manual), not an API response field. The API cannot distinguish a restored input from an empty one. |

### A3 — parity-sweep fixes that do **not** require live confirmation

Found by `elspeth-ed2c2315d7`'s restamp parity sweep, **never live-observed**.
A green live run of a shape that never broke live is not evidence. The tracker
records these as closable on operator authorization; listed here so round 3
does not spend budget re-testing them.

| Ticket | Priority | Filigree status | Fix commit(s) | What must be observably TRUE on the live instance | Concrete repro shape to drive it | Composer web API alone? |
|---|---|---|---|---|---|---|
| `elspeth-38aae1498d` | P2 | `verifying` | `39e419c24` | Nothing new required live. `json_explode` restamps its synthesized `item`/`item_index` declarations, curing a `SchemaConfigModeViolation` that needed no author cooperation. Ships with a DAG corpus manifest hash + case-registry digest refresh — so the **corpus regression watch** in section C covers the ripple. | Only if opportunistically cheap: `json` source → `json_explode` in **fixed/flexible** mode → sink. Otherwise rely on the unit + corpus coverage. | yes (but unnecessary) |
| `elspeth-d1f20e8385` | P2 | `verifying` | `52beec400` | Nothing new required live. All three `llm` transform emit paths restamp declared output field contracts. Provenance side-field (`*_usage`/`*_model`) emission is **pinned unchanged** — so this must not perturb `elspeth-9d13900064`'s observable above. | **Superseded by round-3 finding `elspeth-39118dd24f`** (see the adversarial check, item 3): a fixed/flexible `llm` node whose `schema.fields` include its own response field fails **every row at input validation**, so the emit path this fix touches is never reached. That configuration cannot exercise it at all. The fixed-mode-**without**-response-field configuration remains reachable and is the only live route. Cross-link the two tickets. | **no** for the common configuration — blocked behind `39118dd24f` |
| `elspeth-1f6493861b` | P2 | `verifying` | `090ba13e8` | Nothing required live. `rag_retrieval` restamps across three divergence axes (nullable, `python_type` in the no-results branch, required). | **Likely unauthorable on the acceptance surface** — no retriever is provisioned. Do not attempt. | **no** — no live surface to drive it. |

---

## B. Still-open defects to re-confirm as live

Each has a known reproducing shape. Re-run it, confirm still live, add a dated
datum to the ticket. Do **not** file duplicates.

| Ticket | Priority | Filigree status | Fix commit(s) | What must be observably TRUE on the live instance | Concrete repro shape to drive it | Composer web API alone? |
|---|---|---|---|---|---|---|
| `elspeth-2306940c70` | P2 | **DISCREPANCY** — Filigree: `fixing`, assignee `claude-fable`, claimed 2026-08-04T18:29Z. Tracker roll-up (line 102) and the 01:20 AEST checkpoint both say *"remains triage unclaimed"*. **No fix commit exists** (`git log --grep=2306940c70` across all refs returns nothing) and the ticket has zero comments. Treat as **claimed but unfixed** — the tracker prose is stale by ~6 minutes relative to the claim. | none | Defect still live: after the advisor END gate correctly **withholds** completion on a contradictory revision, the recovery reply falsely asserts the refused instruction is already applied. This is a **reply-honesty** defect — the pipeline state and run are correct. | Re-run the round-2 **adv** probe (session `b2ad4da8`, run `ed16f06b`): build a gate graph routing `amount > 100 → big_amounts`, else `small_amounts`. Then instruct: *"remove the gate entirely but keep the guarantee only amounts>100 reach big_amounts"* → expect FLAG / withheld completion. Then ask to **restore the gate design** and read the reply in `GET /messages`. Defect present if it says the second (contradictory) instruction is "already live and fully valid". | **yes** |
| `elspeth-c0f4193c99` | P3 | `triage`, unassigned | none | Defect still live: the **human rename** path (`UpdateSessionRequest`, `web/sessions/schemas.py:109-117`) has `min_length=1` + `_require_visible_content` but **no `max_length`**, and admits control chars, bidi/zero-width code points, and arbitrarily long values into the same Tier-1 `session.title` column the auto-title gate now protects. After `elspeth-308d1e0831`, the human PATCH path is **looser than the LLM path at the same sink**. | On any session, `PATCH` the title with (a) a 10,000-char string, (b) embedded control characters, (c) a bidi override / zero-width joiner. Each is currently accepted. Contrast against the A1 `308d1e0831` row, where the LLM path now rejects the equivalent shape. | **yes** |
| `elspeth-eca48c1e31` | P3 | `triage`, unassigned | none | Defect still live: **every first message of every new session** triggers a paid LLM completion that bypasses `composer_rate_limit_per_minute` (conceded in the `_auto_title.py` module docstring; OWASP LLM10 unbounded consumption). Session creation is user-controlled. | Create N sessions in rapid succession, each with a first message, and count auto-title provider calls against the composer limiter's window. | **partial** — the API happily accepts the session creations, but the *bypass* is only visible as provider-call volume in CloudWatch / Landscape call records, not in any API response. |
| `elspeth-f1e7679e2a` | P3 | `triage`, unassigned | none | Defect still live but **has no observable live failure**. For `adds_fields` transforms, `output_schema` is built with zero model fields and `extra=allow`, so `model_validate(strict=True)` validates **nothing**; `input_schema` cannot guard created fields either (they never exist on input rows). A created field's declared type is checked nowhere. Post-restamp this is a *silent* wrong contract where it previously crashed with a misleading message. | **None worth driving.** The reachable form (`item: str?` on `json_explode` where the element is a dict) now produces a silently-wrong stamped contract rather than an error — there is nothing for the live instance to show. | **no** — unverifiable live by construction. Do not invent a repro; leave it as a code-level ticket. |

---

## C. Regression watches — green in round 2, must stay green

Each watch is tied to the fix that could plausibly break it, so a failure is
diagnosable rather than merely alarming. Treat any failure here as a
**regression: stop and diagnose, do not file a new defect.**

| Ticket | Priority | Filigree status | Fix commit(s) — *the change that could disturb this watch* | What must be observably TRUE on the live instance | Concrete repro shape to drive it | Composer web API alone? |
|---|---|---|---|---|---|---|
| **g01** (no ticket — regression watch) | n/a | n/a | `0e960ef0e` — **field_mapper restamp**; three chained mappers is the densest exposure of the changed emit path | Completes clean; no `SchemaConfigModeViolation` on any of the three mappers | Linear × 3 `field_mapper` over an **inline `csv`** source. Round-2 baseline: run `1ab41ff4`, 4 source rows, 4 tokens terminal, 4 succeeded, 0 failed | yes |
| **g04** (no ticket — regression watch) | n/a | n/a | `3d77d15bd` — extras walker now **traverses gates**; `937a8010b` — on_error routing | Named routing intact; the poisoned row still quarantines; the run does not abort | `csv` → **gate with named routes** (`premium` / `standard`) + a poisoned row + `on_error` → `bad_rows`. Round-2 baseline: run `43743e6c`; 91/77 → `premium`, 44 → `standard`, `not-a-number` → `bad_rows`, siblings unaffected | yes |
| **g05** (no ticket — regression watch) | n/a | n/a | `3d77d15bd` — walker recurses nested unions; `83a53388a` — **queue guarantee propagation** (coalesce is a fan-in) | All branches merge; no new `graph_structure` rejection introduced by the intersection/abstention semantics | `csv` → **fork → 2 branches → coalesce** → sink. Round-2 baseline: run `9987875a`; 24 node states across 3 rows, all 3 merged rows reached `combined` | yes |
| **g09** (no ticket — regression watch) | n/a | n/a | `83a53388a` — fan-in guarantees; `3d77d15bd` — **Rule B shares the walker**, sinks were the second consumer of the fixed hole | All three sinks still written; no spurious extras rejection at any sink boundary | One source → **three sink plugins** (`csv` + `jsonl` + `text`). Round-2 baseline: run `dd70a319` | yes |
| **g06** (no ticket — regression watch) | n/a | n/a | `39e419c24` — json_explode restamp, which also refreshed the **DAG corpus manifest hash + case-registry digest** | Completes clean; the corpus digest ripple did not disturb the production path | `json` source → transform → sink. Round-2 baseline: run `d76e1ffd` | yes |
| **g07** (no ticket — regression watch) | n/a | n/a | — (baseline coverage, no fix touches it) | Completes clean | `text` source → transform → sink. Round-2 baseline: run `8868e411` | yes |
| **g03b** (no ticket — regression watch; paired with `elspeth-cd0f6a6cd9` in A1) | n/a | n/a | `1efeae10d` — ADR-036 replaced the region-only `deployment` alias with the `aws_textract_profiles` table | The **intended** quarantine split still works and both region-enforcement arms still fire. ADR-036 must not have broken the working path while fixing the alias path | `aws_s3` manifest → Textract → `extracted` / `quarantine` sinks, with one real PDF and one nonexistent object. Round-2 baseline: run `ea9ec481`; real PDF analysed under the deployment-derived region, nonexistent object → `submit_failed` → quarantine | **partial** — needs `GET /api/runs/{id}/diagnostics` for the per-row split |
| **adv** (`elspeth-2306940c70` covers the reply-copy defect) | n/a | n/a | — the **gate** was correct in round 2; only the reply copy is defective (section B) | The FLAG still fires, the repair still converges to CLEAN, and the executed run still proves the routing claim | Advisor END-gate probe: gate graph, contradictory revision → FLAG, repair → CLEAN, then execute. Round-2 baseline: run `ed16f06b`; 250 → `big_amounts`, 40 → `small_amounts`, 900 → `big_amounts` | yes |
| `elspeth-5904b1683a` (**F14 rider**) | P1 | `closed` (2026-08-04, `close_commit release/0.7.2@dc79b5127`) | `872fdf305` + `1fd7b7b8e` (**IN-DEPLOYED**) | **Re-sample ≥10×.** Anything below 10/10 warrants a reopen, not a shrug. Stochastic — never close on a single pass. | Ten fresh plain-guided cold starts: `POST /guided/start`, `profile: live`, **new session and operation id each**, identical canonical intent. Assert HTTP 200 with a non-null composition state; zero `REPAIR_EXHAUSTED`, zero 502s, zero `planner_repair_exhausted`. Round-2 baseline: 10/10 on `173a81cbb` (pre-fix live rate 1/5) | yes |

---

## Discrepancies flagged (do not resolve silently)

| Item | Filigree says | Document says | Disposition |
|---|---|---|---|
| `elspeth-2306940c70` | `fixing`, assignee `claude-fable`, claimed 2026-08-04T18:29Z, **no fix commit, no comments** | Tracker roll-up line 102 and the 2026-08-05 01:20 checkpoint: *"remains triage unclaimed"* | Tracker prose is stale (written 04:35 AEST; the claim landed 04:29 AEST). Ticket is **claimed but unfixed** — it stays in section B as a defect to re-confirm live. |
| `elspeth-454892147c` | `verifying` | Round-2 report "Close dispositions": ***open** — unsampled this round* | Checkpoint 22:38 agrees with Filigree (`verifying`). The round-2 report line is stale prose; Filigree is authoritative. |
| `elspeth-03f5728c33` | `closed`, `close_commit release/0.7.2@3880cb814` | Round-3 brief scope table: ***open** — check status first* | Closed on 2026-08-04 at 15:05Z, after the brief's scope table was drafted. This is the clearest single illustration of why the brief's scope section is superseded. |
| `elspeth-9c01c943a5` (and 3 others) | `close_commit release/0.7.2@6610fa0d4` | `fix_verification` names `cbc0e99d3` | `close_commit` is a release-tip stamp. Fix commits above come from `fix_verification` + the checkpoint log. Same pattern on `558fa5a321`, `03f5728c33`, `5a372d3267`. |
| Round-2 "unsampled" riders | `454892147c` and `49b467d91a` both `verifying` | Round-2 report frames both as awaiting a build | Both fixes were **already in `173a81cbb`** (ancestry-verified). They were undriven, not undeployed. |

## What cannot be confirmed through the Composer web API

- **`elspeth-454892147c`** — needs an induced retryable provider failure inside
  a retry-enabled pipeline. No fault-injection lever exists on the acceptance
  surface; the repro is a local CLI run against `chaosllm`.
- **`elspeth-49b467d91a`** — the fix is frontend input-state restoration; the
  observable is a browser DOM state, not an API field.
- **`elspeth-1f6493861b`** — no retriever is provisioned on the acceptance
  instance, so `rag_retrieval` is unauthorable there.
- **`elspeth-f1e7679e2a`** — the defect is a silent validation *absence*; there
  is no failure for the live instance to exhibit.
- **Partial only:** `elspeth-cd0f6a6cd9` (custody negative needs run
  diagnostics; blocked without the Textract profile grant in the same task-def
  revision), `elspeth-308d1e0831` (counter is CloudWatch),
  `elspeth-eca48c1e31` (bypass is visible only as provider-call volume).

---

# g08 triage — reopen or sibling?

**Recommendation: FILE SIBLING.**

Proposed title: **"Compose-time locked-input extras check is structurally blind
to pass-through fields, and is bypassed by any resolvable node between a
`row_union` and the locked consumer"**

The **pass-through blindness is the primary mechanism** — it is proven without
confound and is proven to be *a* live cause of this run regardless of what else
fired (the run rejected `complaint_text`, which this rule cannot predict under
any topology). The bypass is a proven-reachable secondary.

**A necessary clarification on "reopen".** `elspeth-9d13900064` is at
`verifying`, **not `closed`** — so there is nothing to reopen. `verifying`
means exactly "locally fixed; live acceptance remains", and this run *is* that
live acceptance. It **failed**. So the disposition has two parts, and the
second is not optional:

1. **File the sibling** for the newly-proven wider mechanism (below).
2. **`elspeth-9d13900064` must NOT advance to `closed`.** Add this run as a
   dated negative datum on the ticket. Its narrow root cause is genuinely
   cured and its regression test passes, but the ticket's own headline symptom
   — an LLM provenance side-field breaking a downstream fixed-schema consumer
   at runtime — reproduced verbatim on the deployed fix.

This is the honest reading of the tension: the *mechanism* the ticket
root-caused is fixed, while the *symptom* the ticket is named for is still
live through a different path. Calling it a sibling is a statement about cause,
not a discharge of the ticket.

Evidence: round-3 run `dc689cfe-9f4b-47ba-bd04-abd04309debc`, session
`04ff99fb-4147-4405-878d-c70a9be68e02`, graph g08, on the deployed fix.
Compose succeeded, `/validate` returned `is_valid=true`, run status `failed`
with `PluginContractViolation` (5 × `extra_forbidden`) at
`transform_final_cleanup_3981d1107594`.

## The deduction that settles it

`elspeth-9d13900064` is **fixed** and its own regression test
(`test_row_union_extras_reach_a_fixed_mode_field_mapper_consumer`, added by
`3d77d15bd`) still passes. The live failure did not travel the fixed path:

1. The runtime rejected `one_sentence_summary_usage` with `extra_forbidden`.
   A generated input model gets `extra="forbid"` only when `config.mode` is
   not `flexible`/`observed` — `schema_factory.py:167`, with `is_observed`
   short-circuiting to `extra="allow"` at `:119-121`. So the consumer's
   `schema.mode` was **`fixed` with declared fields**.
2. That is exactly the predicate `_locked_input_field_set` tests
   (`web/composer/state.py:1955`), so `consumer_locked_input` was **not None**
   — the consumer-side gate of Rule A was satisfied. **Confirmed empirically,
   closing the "maybe field_mapper derives its input model from something else"
   escape**: `field_mapper` builds `FieldMapperInput` via `_create_schemas` →
   `create_schema_from_config(cfg.schema_config, "FieldMapperInput")`
   (`plugins/infrastructure/base.py:663`) from the *same* declaration
   `_locked_input_field_set` reads. Probed across four option shapes — runtime
   `extra` is `forbid` **only** in `fixed` mode, and in exactly that case the
   composer returns the identical field set; `flexible`/`observed` give
   `allow`/`None` on both sides. `select_only` changes neither. There is no
   third mechanism here.
3. `one_sentence_summary_usage` **is** in the composer's predicted emit set for
   an `llm` arm. Verified empirically by constructing the probe transform the
   walker uses (`_producer_emit_set` → `_output_schema_config.guaranteed_fields`,
   `state.py:2815-2818`): under **every** authored schema mode the arm predicts
   `('one_sentence_summary', 'one_sentence_summary_model', 'one_sentence_summary_usage')`.
4. Therefore, had the fixed boundary path run with the arm emit set,
   `extras = producer_emit - consumer_locked_input` would have been non-empty,
   `_locked_input_extras_error` would have returned an entry, and `/validate`
   would have failed.

`/validate` returned `is_valid=true`. **So the arm emit set never reached the
Rule A comparison.** This is not the boundary `3d77d15bd` covers.

## Answers to the four questions

**1. What does the check cover, and where is it invoked?**
`3d77d15bd` adds an extras-polarity walker to `_check_schema_contracts` in
`web/composer/state.py` — one function, reached by the ordinary
`CompositionState.validate()` path, so it *does* run on freeform compose.
It unions `row_union` arm emit sets (`_row_union_definite_emits`), traverses
**gates only** (`_connection_definite_emits`), recurses nested unions, and
contributes **∅ for `queue` and `coalesce`** — the fix's own comment calls
extending it to queues "a drop-in branch here, left to the track that owns
queue contract semantics." Rule A's boundary path is entered **only when the
presence walk abstained** (`if actual_producer is None:`, `state.py:3189`);
`_producer_entry_row_union_boundary` returns `None` for any intermediate node
that is neither a `gate` nor a `row_union`.

**2. Does the provenance-vs-ordinary-field distinction explain it? Partly —
and this is the PRIMARY mechanism (no confound).**
The subtraction itself is kind-agnostic. But its *input* is not:
`_producer_emit_set` returns the transform's own `guaranteed_fields`, never the
fields it passes through from upstream. Reproduced locally — with the source
declaring `complaint_id`/`complaint_text` as guaranteed and the mapper
accepting only `verdict`, the reported extras were still exactly
`('verdict_model', 'verdict_usage')`. Even an `llm` arm authored in `fixed`
mode *declaring* `complaint_id`/`complaint_text` predicts only the trio.
**So the live rejection of `complaint_text` could never have been predicted by
this rule under any topology.** That is a gap `3d77d15bd` never claimed to
close, and it is wider than the ticket.

**3. Is the failing node across a `row_union` boundary? (secondary mechanism)**
Not determinable from the evidence supplied — the authored graph is live-only
and no round-3 artifact exists in the repo. But it does not need to be: step 4
of the deduction proves the union's arm emits never reached the check, which
means the mapper is **not** a gate-only-chained direct consumer of the union.
The masking mechanism is reproduced: inserting **one** non-locked
(`mode: observed`) `field_mapper` between the `row_union` and the fixed-mode
consumer takes `actual_producer is None` to False, so the boundary path is
skipped and `_producer_emit_set` returns only the relay's own guarantees —
`locked_input_extras` went from `[('verdict_model','verdict_usage')]` in the
control to **`[]`**. g08's graph carries auto-wired safety controls
(`prompt_shield_auto_*`, `safety_arm_*`), so an intervening resolvable node is
the expected shape, not an exotic one.

**Confound — read this before citing the reproduction.** That repro also
emitted a `transform_contract_violation` on the downstream mapper ("with
`select_only: true` the mapping will only emit [(none)]"): the observed-mode
relay did not merely bypass the extras rule, it made `verdict`'s arrival
unprovable, collapsing the downstream contract picture more broadly. The live
g08 validated **fully green** — no `transform_contract_violation` — so the
reproduction demonstrates that the bypass is **reachable**, *not* that it is
what fired live. The pass-through blindness of Q2 — the primary mechanism —
carries no such confound and needs no assumption about the live topology.

**4. `integrity.closure: "open"` — a second finding, not yet decided.**
`closure` is `"closed"` only when `emitted == terminal` and
`missing_terminal_outcomes == 0` (`web/execution/accounting.py:194-200`).
Here emitted=4, terminal=3, missing=1, and notably **failed=0** — the row that
hit the mapper failure recorded *no* terminal outcome at all: not
`(FAILURE, ON_ERROR_ROUTED)`, not the enumerated `(FAILURE, UNROUTED)` pair.
The accounting is *honest* about it (that is the fail-closed posture working),
so this is not a data-integrity breach. Whether abort-with-stranded-tokens is
the intended contract for a fatal transform input-validation failure is a
separate question from this triage; **track it as its own observation** rather
than folding it into the reopen/sibling call.

**Independently corroborated.** The parallel g11 triage (appended to
`round3-graph-corpus.md`, run `a4f534df`, session `11abed43`) reached the same
conclusion from a different graph and proposes it as its **F2**: *"Input-validation
`PluginContractViolation` raises without recording a terminal outcome, leaving
the token pending and run integrity `open`"* (P2), noting it is an **engine**
defect at the raise site and not llm-specific. Two independent graphs on two
different builds exhibiting it settles the "is this expected?" question: **it is
a defect, not the intended abort contract.** File it once — do not duplicate
across the two triages.

## Distinguishing mechanism, stated for the sibling ticket

| | `elspeth-9d13900064` (fixed) | Proposed sibling |
|---|---|---|
| **Fields at issue** *(primary — no confound)* | plugin-`guaranteed_fields` only: the LLM provenance trio | additionally **pass-through** upstream fields, which `_producer_emit_set` structurally omits under *every* authored schema mode — so they are unpredictable by this rule at any topology |
| **Presence walk** *(secondary — reachable, not proven live)* | **abstains** at the `row_union`, so the new boundary path is entered | **resolves** to an intermediate node, so the boundary path at `state.py:3189` is never entered at all |
| Cure shape | re-resolve the union boundary in the extras polarity | thread an accumulated pass-through emit set through resolvable intermediates, and enter the boundary walk independently of whether the presence walk abstained |

## The one experiment that would pin which arm fired

Fetch the authored composition state for session
`04ff99fb-4147-4405-878d-c70a9be68e02` and read two things: **(a)** the node
type and plugin of whatever produces `final_cleanup`'s input connection, and
**(b)** `final_cleanup`'s `schema.fields`. If (a) is anything other than the
`row_union` reached through gates only, the masking arm fired; if (a) *is* the
union, then the pass-through arm fired alone and the sibling narrows to the
emit-set model. Either way the recommendation is unchanged — both mechanisms
sit outside `3d77d15bd`.

---

# Adversarial check of the round-3 report

Target: `docs/acceptance/2026-08-05-battery-round-3-report.md` @ `e77e199f3`.
Read-only; no AWS, code or tracker state touched. Verdicts are deliberately
unsoftened — two items are OVERCLAIMED and one of those is load-bearing for an
operator sign-off decision.

**Summary: 2 UPHELD, 3 OVERCLAIMED, 1 CANNOT DETERMINE (blocking).** The two P1
tickets survive intact; the ADR-036 headline and two "confirmed live" verdicts
do not. Item 6 was not on the list I was given but sits in the same section and
must be settled before sign-off.

**The single most important correction is 5a**, because it is the one that
changes an operator decision: `elspeth-9c01c943a5` is listed as confirmed on
**1 of the 3 samples the round's own corpus mandates**, against a defect round 2
observed at 1-in-2.

## 1. ADR-036 "confirmed live, both halves" — OVERCLAIMED (conclusion survives, stated reason does not)

The **conclusion is right**; the **discriminator the report leans on is not the
thing that establishes it**.

What is sound: `_process_single_with_state` verifies the bucket region
**unconditionally**, for both location modes
(`textract_document_analysis.py:770-778`) — so an alias carried as a literal
bucket would HeadBucket a nonexistent name and fail-close as
`bucket_region_unverified` (`:776`). Round 2 observed exactly that (run
`e41d0e6b`). So "not `bucket_region_unverified`" does rule out *alias-as-literal*.

Three problems with resting the claim there:

1. **`service_error` can absolutely be produced by a mis-bound profile.** A
   profile bound to a *real but wrong* bucket passes HeadBucket, passes the
   region check, then fails `StartDocumentAnalysis` — landing on
   `{"reason": "submit_failed", "error_type": "service_error"}`
   (`:832-837`). The negative therefore excludes one specific mis-binding, not
   mis-binding in general. The report's phrasing ("would have meant the alias
   was still being HeadBucket-ed as a literal") is *literally* true but is
   presented as if it certified correct binding.
2. **Post-ADR-036 the negative is guaranteed by construction.** The allowlist
   projection makes `bucket`/`bucket_field` web-inexpressible and the
   `profile_alias_used_as_bucket` guard (`web/plugin_policy/validation.py:119`)
   rejects the alias at compose time. An outcome that *cannot* occur is not a
   discriminating observation.
3. **Precision:** `service_error` is an `error_type`, not a `reason`. The
   `reason` is `submit_failed` — the same code round 2 recorded for its
   nonexistent object. Reporting the `error_type` as though it were the failure
   reason makes the round-2/round-3 comparison harder than it needs to be.

**The honest claim**, which is *stronger* than the one written: the profile
bound correctly because **the real document was successfully analysed** — a
success requires the correct bucket, a matching region, and a readable object.
The absence of `bucket_region_unverified` is corroborating colour, not the
proof. Rewrite the bullet to lead with the positive.

**And that positive is validator-guaranteed, not transcribed.** Run
`67d40936` returned **HTTP 200** with status `completed_with_failures`, and
`web/execution/schemas.py:491-492` refuses to project that status unless
`accounting.tokens.succeeded > 0` (this is the very check item 4 is about). So
`succeeded ≥ 1` is enforced by the endpoint that served the response — it does
not rest on reading "1/1" off a table. Cite the 200 itself as the evidence.

## 2. Custody NFR — OVERCLAIMED (the test is non-probative)

**The search cannot distinguish, and would have passed before ADR-036 too.**
Retract it.

- The ADR's custody assertion is about **call records** — the NFR test is
  `test_profiled_textract_runtime_uses_private_binding_only_for_aws_calls`
  (`tests/unit/web/execution/test_preflight_side_effects.py:418-420`), whose
  docstring scopes it to "persist ZERO **call records** containing the operator
  bucket literal". That is the audit DB, not a web projection.
- The web projections searched **structurally cannot carry it**.
  `RunDiagnosticsResponse` (`web/execution/schemas.py:965`) does not project
  call records at all; the comment at `:955` states the payload "lives in the
  audit DB under `calls.response_ref`". The only route by which a bucket
  literal could reach `/outputs` or `/results` is **row data** — and in profile
  mode the bucket is never in row data *by construction*.
- **Would it have passed pre-ADR-036?** In the actual observed pre-state
  (round 2, run `e41d0e6b`), the composer authored the **alias**
  `acceptance-docs` as the bucket value — so the *real* bucket literal was
  absent from row data then as well. A search for the real literal returns zero
  in both worlds.

The search does establish something real, but it is the *authoring* half —
that no location vocabulary reached row data — which the report already claims
separately from the composed graph. It is not independent evidence.

**Honest claim:** "Custody NFR — **not verified live.** The ADR's assertion is
over Landscape call records, which could not be queried remotely. The web-API
search is not a substitute: those projections never carry call identity." The
report's own scope-limit sentence is correct and should be promoted from a
trailing caveat to the verdict.

## 3. `elspeth-39118dd24f` (P1) — UPHELD

The discriminator is exactly right, verified by direct construction of the
transform's input model:

| authored `schema` | `Input.extra` | required inputs |
|---|---|---|
| `observed` | `allow` | `[]` |
| `flexible` + response field in `fields` | `allow` | `['complaint_text', 'summary']` |
| `fixed` + response field in `fields` | `forbid` | `['complaint_text', 'summary']` |
| `fixed`, response field **omitted** | `forbid` | `['complaint_text']` |

`summary` is the node's own `response_field`, and it is demanded as a
**required input** in both `fixed` and `flexible`. `observed` is genuinely
immune — `create_schema_from_config` short-circuits to a dynamic schema with no
fields at all (`plugins/infrastructure/schema_factory.py:119-121`), so g08/g10
passed for the stated reason and not by luck. The fourth row confirms the
discriminator is precisely "`schema.fields` contains the node's own response
field", not merely "mode is fixed/flexible".

**P1 is justified**, on failure totality rather than frequency: every row of an
affected node fails, and the affected configuration is one the project already
treats as real — `elspeth-d1f20e8385` (`52beec400`) exists *specifically* to fix
restamping for "an LLMTransform with mode: fixed/flexible and authored fields".

**Worth adding to the ticket:** this narrows `elspeth-d1f20e8385`'s reachable
surface. Its fix only matters on the emit path, which is downstream of input
validation — so for the fixed/flexible-**with**-response-field configuration,
that fix is unreachable in production until `39118dd24f` lands. The
fixed-**without**-response-field configuration (row 4) remains reachable, so the
fix is not wholly dead — but this pairing should be cross-linked.

## 4. `elspeth-47fa7c01eb` (P1) — UPHELD, and the open question is now ANSWERED: YES, reachable

Both readings confirmed:

- **Engine permits it.** `contracts/run_result.py` carries an explicit,
  commented arm: `case (RunStatus.COMPLETED_WITH_FAILURES, _, True, False, True): return`
  — annotated *"All-quarantined or success-plus-quarantine, no uncaught
  failures."* With `rows_succeeded=0, rows_quarantined=N`:
  `terminal_clean_indicator=True`, `failure_indicator=(N−N)>0=False`,
  `has_quarantine=True` → legal `COMPLETED_WITH_FAILURES`.
- **Web forbids it.** `web/execution/schemas.py:491-492` re-derives the
  invariant as `if accounting.tokens.succeeded <= 0: raise`, dropping the
  quarantine disjunct.

**The reachability question the report could not answer:** an all-quarantined
run yields `tokens.succeeded == 0` at the web layer, because quarantined tokens
are recorded as `TerminalOutcome.FAILURE` with path `QUARANTINED_AT_SOURCE` and
increment `failed_tokens` and `quarantined` — **never** `succeeded_tokens`
(`web/execution/accounting.py:128-140`). So the combination is reachable
end-to-end, and the "web dropped an arm" framing is correct as a general
mechanism. It is not a hypothesis.

Two caveats that belong on the ticket, neither of which undermines it:

- The validator is **not new** — `"requires tokens.succeeded > 0"` is present in
  round 2's deployed `173a81cbb` at the same line. This is a long-standing
  divergence the round happened to trip, not a fix-wave regression.
- The ticket's own open question (was `quarantined == 0` for `c69b6ab6`?)
  stays genuinely open and is correctly flagged there. If it was zero, the web
  layer was *correctly* refusing an illegal state and the second defect the
  ticket posits is the real one for that instance — but the general mechanism
  above stands regardless. **The report's confident framing outruns the
  ticket's honest hedge; prefer the ticket's wording.**
- Possible corroboration worth one query: round 2's `e41d0e6b` was described as
  "all rows quarantined", which is this exact shape on the previous build.
  Whether it 500'd then is unrecorded.

## 5. Per-graph table — OVERCLAIMED in two specific places

**5a. `elspeth-9c01c943a5` is listed under "Fixes confirmed live" on a single
sample. This is the most consequential error in the report.**

The round's own corpus mandates the opposite: g08 is *"Intermittent at 1-in-2,
so a single clean pass proves nothing"* and *"Run this three times in three
fresh sessions (`g08-s1`, `g08-s2`, `g08-s3`)"*
(`round3-graph-corpus.md:435-436, 445`). The report's table records **one** g08
run (`dc689cfe`). Round 2 observed the raw 500 on 1 of 2 attempts, so a single
clean pass is consistent with the defect being fully present.

**Honest claim:** `elspeth-9c01c943a5` — **not confirmed; 1 of 3 required
samples clean.** Move it out of "Fixes confirmed live". This matters because
the report drives sign-off on a P1, and stochastic items are explicitly never
closed on a single pass.

**The remedy is cheap and the report already names it.** g08 got one sample
because of the round's own methodology finding — composes 422 under
concurrency. So the fix is **re-drive g08 serially ×3**, not "accept the single
pass". Serial composes demonstrably succeed (g01: 197s / HTTP 200), so the two
missing samples cost wall-clock, not a redeploy.

**5b. The PASS verdicts are sound for integrity but not for semantics.**

The defensible half: for `status == "completed"` the web validator already
enforces `closure == "closed"`, `succeeded > 0` and `failed == 0`
(`schemas.py:478-484`), so "terminal state `completed`" is a stronger claim than
it looks and does foreclose the stranded-token shape of `elspeth-82d4c5146c`.

The gap: round 2 additionally verified **routing destinations**, and round 3 did
not. Round 2's g04 recorded *"91/77 → `premium`, 44 → `standard`, `not-a-number`
→ `bad_rows` quarantine, siblings unaffected"*; round 3's g02 records only
`completed_with_failures 4/1/1`. A named-routing regression that sent every row
to `standard` would still produce 4/1/1 and still be marked **PASS**. Likewise
g03 and g06 carry no token accounting at all — just `completed`.

**Honest claim:** g02/g03/g04/g06 are **integrity passes, not behavioural
passes**. Either re-drive with per-destination assertions, or label them
"terminal-state PASS; routing destinations unverified this round" so round 4
knows the regression net has a hole in it.

## 6. `elspeth-03f5728c33` "observed working incidentally" — CANNOT DETERMINE, and it blocks

Not in the five I was asked to check, but it is the same class as 5a and it
sits in "Fixes confirmed live", so it must be settled before sign-off.

The report's evidence: *"on g01, `/validate` named two pending interpretation
reviews that the `?status=pending` listing reported as empty. That is precisely
the backstop."*

Now read the ticket's **defect** signature: *"`/validate` then fails
interpretation_review while `GET /interpretations?status=pending` returns
nothing user-actionable."* Those are the same sentence. The fix's entire point
is that `/validate` runs the surfacer in repair mode **before** delegating, so
that afterwards the listing is **non-empty**.

So the observation is either the fix or the defect, depending entirely on
**query order**:

| Order | Meaning |
|---|---|
| `?status=pending` read **before** `/validate` | Empty is expected pre-surfacing. Proves nothing either way. |
| `?status=pending` read **after** `/validate` | The backstop did **not** surface. The report has recorded a **reproduction** as a confirmation. |

**What would settle it:** the driver log's request ordering for that g01
session — specifically whether the empty `?status=pending` response precedes or
follows the `/validate` call, and whether a second `?status=pending` read was
taken after `/validate`. If no post-`/validate` read exists, the claim is
unsupported regardless of order and should move to "Not confirmed".

I cannot resolve this from the report or the code; it needs the round's own
transcript. **Treat as blocking** — if it resolves the wrong way it is a second
confirmation to retract, next to `9c01c943a5`.

## What I could not fault

Items 3 and 4 survive attack. The "Not confirmed" section is honest and, in the
`elspeth-9d13900064` case, notably disciplined — `verifying` is not `closed`
and the report says so. The methodology finding (do not parallelise composes)
is properly controlled by the serial g01 re-drive.
