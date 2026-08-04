# Acceptance battery round 3 — graph corpus

Date written: 2026-08-05. Companion to
`docs/acceptance/2026-08-05-battery-round-3-brief.md` (scope) and
`docs/acceptance/2026-08-04-battery-round-2-report.md` (what round 3 re-covers).

This document is the **authoring input** for round 3: twelve graphs, each with
the exact natural-language intent to send, the topology it should produce, the
terminal outcome to expect, and the gates the driver will hit. It changes no
code and asserts nothing about the deployment — run it against a stack that has
already satisfied the brief's preconditions.

---

## Summary — round 3 → round 2 coverage map

| id | Targets | Re-covers r2 | Ticket under test |
|---|---|---|---|
| g01 | linear multi-transform | g01 | — (regression watch) |
| g02 | gate, named routes, poisoned row, `on_error` | g04 | — (regression watch) |
| g03 | fork → 2 arms → coalesce | g05 | — (regression watch) |
| g04 | `json` source + `json_explode` | g06 | — (regression watch) |
| g05 | `text` source + `text` sink | g07 | — (regression watch) |
| g06 | sink variety (csv + jsonl + text) | g09 | — (regression watch) |
| g07 | profile-first Textract (ADR-036) | g03b | `elspeth-cd0f6a6cd9` (fixed) |
| g07b | alias-as-bucket trap probe | — | `elspeth-cd0f6a6cd9` (fixed) |
| g08 | 2-LLM-arm A/B via `row_union`, ×3 samples | g02b | `elspeth-9c01c943a5` (fixed), `elspeth-9d13900064` (open) |
| g09 | 4 LLM nodes → ≥4 auto-wired controls | — | `elspeth-558fa5a321` (fixed) |
| g09b | compose cancelled mid-loop | — | `elspeth-03f5728c33` (fixed) |
| g10 | LLM enrichment → fixed-schema mapper | g10 | `elspeth-ed2c2315d7` (open) |
| g11 | `llm` source, dynamic schema | g08 | `elspeth-5a372d3267` (open) |
| g12 | advisor END-gate contradiction | adv | `elspeth-2306940c70` (open), `elspeth-49b467d91a` (verifying) |

Round-2 ids **g01–g10 do not correspond** to round-3 ids of the same number.
Use this table when writing the report table, not the numbering.

`elspeth-454892147c` (retry exhaustion) has no graph. It still needs a
fault-injection lever that the acceptance surface does not provide; report it
unsampled rather than approximating it.

---

## How to send an intent

Each fenced block below is the **message body content**, verbatim. The route is
`POST /api/sessions/{id}/messages` and the request model is
`SendMessageRequest` (`src/elspeth/web/sessions/schemas.py:133`), so the body
file is:

```json
{"content": "…the fenced intent, as a single JSON string…"}
```

`content` is the only required field (`min_length=1`, `max_length=65536`);
`state_id` is optional and should be omitted for a first turn. Then:

```bash
python scripts/acceptance_battery.py api POST /api/sessions/<sid>/messages body.json g01/compose.json
python scripts/acceptance_battery.py resolve-reviews <sid> g01
python scripts/acceptance_battery.py run-graph <sid> g01
```

A client timeout does **not** abort the server-side compose — re-read
`GET /api/sessions/{id}/messages` to recover the outcome. This is deliberate in
g09b and accidental everywhere else.

### Intents are reconstructed, not replayed

The round-2 driver wrote its per-graph archives to `--state-dir`, defaulting to
`/tmp/elspeth-battery`. **That directory no longer exists**, and round 2's
verbatim intent strings were not committed. Every intent below is *reconstructed
from the round-2 report's per-graph description*, not replayed.

The consequence matters for the four still-open P2s (g08, g10, g11, g12): if a
defect **does not** reproduce against a reconstructed intent, that is weak
evidence. It is consistent with a fix, but also with the reconstruction simply
not steering the Composer down the same path. Do not write a non-reproduction up
as a discharge — record it as "not reproduced against a reconstructed intent"
and leave the ticket open. A defect that **does** reproduce is full-strength
evidence regardless of provenance.

Round 3 should archive its own intents alongside the report so round 4 replays
rather than reconstructs.

---

## The two execution gates, mechanically

The driver handles both. Understanding *why* each fires prevents misreading a
gate as a defect.

### Gate 1 — pending interpretation reviews

`POST /validate` refuses while any interpretation requirement is unresolved.
The kinds are a closed enum (`InterpretationKind`,
`src/elspeth/contracts/composer_interpretation.py:74`): `vague_term`,
`invented_source`, `llm_prompt_template`, `pipeline_decision`,
`llm_model_choice`.

**Every graph in this corpus stages at least one review**, because every graph
asks the Composer to invent its own sample rows — that is the `invented_source`
path, and the csv/json/text sources all carry composer hints instructing the
planner to stage it. LLM-bearing graphs additionally stage
`llm_prompt_template` per LLM node.

A compose **correction** can stage further reviews, so re-run `resolve-reviews`
after any repair turn until it reports zero.

> `resolve-reviews` hardcodes `{"choice": "accepted_as_drafted"}`. That is
> correct for g01–g11. **Do not run it blanket on g12**, whose whole point is
> inspecting a retained draft before accepting it.

### Gate 2 — the 428 LLM-fanout acknowledgement

The brief's shorthand ("two or more LLM nodes trigger a 428") is **not the
implemented rule**, and following it will mispredict half this corpus. The
actual logic is `evaluate_execution_fanout_guard`
(`src/elspeth/web/execution/fanout_guard.py:170-239`):

1. Only nodes with `node_type == "transform"` **and** `plugin == "llm"` are
   considered (`:189`). The **`llm` *source* never counts** — it is a source
   node. Auto-wired safety controls (`aws_bedrock_prompt_shield`,
   `aws_bedrock_content_safety`) never count either — they are not `plugin ==
   "llm"`.
2. Such a node raises a risk when **any** of: upstream cardinality is unknown;
   an upstream **fanout marker** exists; or estimated provider calls exceed
   `LLM_FANOUT_HIGH_CALL_THRESHOLD = 100` (`:203-207`).
3. A fanout marker (`_fanout_marker_for_node`, `:405`) is a token-creating
   transform (`creates_tokens = True` — `json_explode`, `line_explode`,
   `blob_csv_expand`), an aggregation with `output_mode == "transform"`, or
   **a gate with `len(fork_to) > 1`** — i.e. a fork.
4. The guard fires when **≥1** risk exists (`:238`), not ≥2.

So the 428 is really *"an LLM transform whose input volume the composer cannot
bound"*. This is why round-2 g02b (forked arms feeding two LLM nodes) got the
428 while round-2 g10 (one LLM node on a small CSV) did not — it was the fork
upstream, not the arm count.

Predicted per graph: **g08 and g09 expect a 428**; **g10, g11 and g12 do not**
(g10 is one LLM transform over ~5 known rows; g11's LLM is a source; g12 has no
LLM node). g01–g07 have no LLM transform at all.

Operationally: an unexpected 428 is **not** a defect — the driver acks it and
continues. An absent 428 is not a defect either. The only defect shape here is a
**428 carrying no `ack_token`**, which strands the driver.

---

## Authoring expectations that apply to every graph

These are contract requirements the Composer must satisfy on its own. A
validation rejection naming one of them is a **Composer defect**, not an
authoring omission in this corpus — none of the intents mention them, because no
real user would.

- **Every sink requires `on_write_failure`.** It has no default; omitting it
  fails validation. Either `discard` or a quarantine sink name.
- **Every `csv`/`json`/`text` sink in `mode: write` requires an explicit
  `collision_policy`** (`fail_if_exists` / `auto_increment`). The composer
  rejects an implicit one; `auto_increment` is the safe choice.
- **Every source requires `on_validation_failure`** — a quarantine sink name or
  explicit `discard`.
- **A quarantine sink must be `csv` or `json`, never `text`.** The `text` sink
  writes exactly one configured field and is documented as unable to preserve a
  rejected row losslessly ("not a generic failure sink"). A `text` quarantine
  sink is a defect worth filing.
- **A `text` sink needs exactly one string field**, so it should be preceded by
  a `field_mapper` that selects it. This matters in g05 and g06.

Verdicts come from `GET /api/runs/{id}/diagnostics` — per-token node states,
error `reason` codes, sink attribution. Never from output-file mtimes.
`completed_with_failures` is a **healthy** terminal status when rows were
quarantined by design (g02, g07).

---

# The graphs

## g01 — linear multi-transform

**Purpose:** prove the simplest shape still composes and runs clean.
**Re-covers:** round-2 g01 (GREEN, run `1ab41ff4`). A failure here is a
**regression** — stop and diagnose, do not file a new defect.

```
I've got a small list of support tickets — ticket id, customer name, a priority word, and a free-text summary. Build me a pipeline that reads them, renames the columns to tidy snake_case names, lowercases the priority word so it's consistent, and shortens each summary to about 80 characters so the output stays readable. Write the result to a CSV I can open in Excel. Please make up four sample tickets so I can see it work end to end.
```

**Expected topology:** `csv` source → `field_mapper` → `value_transform` →
`truncate` → `csv` sink. Linear, four nodes plus source and sink. Round 2 used
three `field_mapper` nodes; three *different* transforms is the same topology
with wider plugin coverage. If the Composer picks three mappers instead, that is
equally acceptable — the property under test is the linear chain.

**Expected outcome:** `completed`. Diagnostics: 4 source rows, 4 terminal
tokens, 4 succeeded, 0 failed; every token passes through all three transform
nodes in order; one sink attribution per row.

**Gates:** Gate 1 only (`invented_source`). No 428 — no LLM transform.

> "about 80 characters" is safe wording: the routing-threshold detector
> (`_STATED_THRESHOLD_PATTERN`, `pipeline_planner.py:988`) ignores a number
> followed by a unit noun, and this clause has no routing verb. It will not be
> misread as a gate condition.

---

## g02 — gate with named routing, a poisoned row, and `on_error`

**Purpose:** named-route gate, source-validation failure, and quarantine
routing in one graph.
**Re-covers:** round-2 g04 (GREEN, run `43743e6c`). Failure = regression.

```
We score our suppliers out of 100. Read a short supplier list — supplier name and score — and split it two ways: anything scoring above 75 goes to a high_performers file, and everything else goes to a review_queue file. The scores are pasted out of a spreadsheet and occasionally have junk in the score column, so if a row's score isn't a real number I want it to land in a separate bad_rows file rather than killing the whole run. Make up about five suppliers, and include one with a junk score so I can watch the quarantine work.
```

**Expected topology:** `csv` source (numeric `score`, `on_validation_failure` →
`bad_rows`) → `gate` with two named routes → `high_performers` sink +
`review_queue` sink; plus a `bad_rows` quarantine sink (`csv` or `json`).

**Expected outcome:** `completed_with_failures` — healthy. Diagnostics: **each
row lands on the side its own score implies** (above 75 → `high_performers`,
otherwise → `review_queue`) — the Composer invents the five suppliers, so do not
expect a fixed distribution; and exactly one row failing at the **source**
boundary with a validation `reason` code, attributed to `bad_rows`. Assert
siblings are unaffected: the poisoned row must not suppress any other row's
terminal state.

**Gates:** Gate 1 only. No 428.

> "anything scoring above 75 goes to a high_performers file" deliberately binds
> comparison wording to a bare number inside a clause with a routing verb, which
> is exactly what `_stated_threshold_in` recognises as an authoritative routing
> threshold. This exercises the stated-threshold path rather than leaving the
> gate condition to the planner's invention.

---

## g03 — fork into two branches, then coalesce

**Purpose:** parallel branches merged back to one row per input.
**Re-covers:** round-2 g05 (GREEN, run `9987875a`). Failure = regression.

```
Take a short list of products with a sku, a name and a price. I want two things worked out from each product at the same time — one branch that builds a display label out of the name and the sku, and another that rounds the price to whole dollars and adds a currency suffix — and then I want both results brought back together into a single row per product before anything is written. Save the merged rows to one CSV called combined. Make up three products.
```

**Expected topology:** `csv` source → `gate` with `fork_to` of length 2 → two
transform branches (`field_mapper` / `value_transform` or `type_coerce`) →
`coalesce` node → `csv` sink named `combined`.

**Expected outcome:** `completed`. Diagnostics: 3 source rows; node-state count
consistent with both branches executing for every row (round 2 saw 24 node
states across 3 rows); all 3 merged tokens terminal at `combined`, and **no**
row reaching the sink twice — a duplicate is a coalesce defect.

**Gates:** Gate 1 only. No 428 — the fork *is* a fanout marker, but there is no
LLM transform downstream for it to apply to.

> Branch wording is deliberately non-conditional ("rounds the price", not "if
> the price is over N"). Conditional phrasing would steer the planner toward a
> routing gate instead of a fork, and this graph is testing the fork/coalesce
> pair.

---

## g04 — `json` source with a token-creating explode

**Purpose:** JSON ingest plus row multiplication.
**Re-covers:** round-2 g06 (GREEN, run `d76e1ffd`). Failure = regression.

```
I have a JSON file of orders. Each order has an order id, a customer name, and a list of line items, where each item has a product and a quantity. Read it and flatten it out so every line item becomes its own row, carrying the order id and customer name alongside the item's own fields. Save the flattened rows as JSON Lines. Make up three orders with a couple of items each.
```

**Expected topology:** `json` source → `json_explode` → `json` sink
(`format: jsonl`).

**Expected outcome:** `completed`. Diagnostics: 3 source rows expanding to the
full line-item count (6–8 depending on what the Composer invents); assert
**terminal tokens > source rows**, which is the property `json_explode`
(`creates_tokens = True`) exists to provide. Every child token must carry the
parent's `order_id` and customer name — assert on a row, not just the count.

**Gates:** Gate 1 only. No 428 — `json_explode` is a fanout marker, but no LLM
transform consumes it.

---

## g05 — `text` source and `text` sink

**Purpose:** line-oriented ingest and single-field line-oriented output.
**Re-covers:** round-2 g07 (GREEN, run `8868e411`). Failure = regression.

```
I've got a plain text file with one news headline per line. Read it a line at a time, tidy each headline into title case, and write the tidied headlines back out to a text file, one per line. Invent about six headlines.
```

**Expected topology:** `text` source (`column` naming the line field, e.g.
`headline`) → `value_transform` → `text` sink (`field` set to the transformed
field).

**Expected outcome:** `completed`. Diagnostics: 6 source rows, 6 terminal
tokens, **0 diversions**. The `text` sink rejects values containing CR or LF and
diverts values not representable in the configured encoding, so a non-zero
diversion count here means the transform introduced a line break — worth a
defect.

**Gates:** Gate 1 only. No 428.

> The `text` sink writes exactly one configured string field. Because this graph
> carries a single field end to end, no selecting `field_mapper` is needed —
> unlike g06.

---

## g06 — sink variety from one source

**Purpose:** three sink plugins fed from a single source.
**Re-covers:** round-2 g09 (GREEN, run `dd70a319`). Failure = regression.

```
From one small CSV of employees — name, department and email address — I want three outputs at the same time: the full records as a CSV, the same records as JSON Lines for the data team, and a plain text file containing just the email addresses, one per line. Make up five employees.
```

**Expected topology:** `csv` source → three sink paths: `csv` sink; `json` sink
(`format: jsonl`); and a `field_mapper` selecting only `email` → `text` sink
(`field: email`).

**Expected outcome:** `completed`. Diagnostics: 5 source rows; **all 5 rows
reaching each of the three sinks**; 0 failed; 0 diversions on the text path.

> Assert the per-sink coverage, not a single total. Whether diagnostics record
> one attribution per row per sink, per token, or per sink-write is not pinned
> here — read the actual shape from round-2's `dd70a319` diagnostics if an exact
> count is wanted.

**Gates:** Gate 1 only. No 428.

> The interesting authoring question is whether the Composer inserts the
> selecting `field_mapper` before the `text` sink. The `json` sink's own
> composer hint states the rule explicitly ("route the final path through
> `field_mapper(select_only=true)` before this sink; a sink named cleanup is not
> a cleanup transform"). If instead the Composer routes full rows into the
> `text` sink and relies on `field`, check whether the run diverts — a silent
> field drop is a finding.

---

## g07 — profile-first Textract (ADR-036)

**Purpose:** confirm Textract binds its document location through the operator
profile, with rows carrying relative keys only.
**Re-covers:** round-2 g03b (GREEN as designed, run `ea9ec481`).
**Ticket:** `elspeth-cd0f6a6cd9`, fixed by ADR-036 / `1efeae10d`.

```
We have documents available through the acceptance-docs document profile. Build me a pipeline that runs Amazon Textract over a couple of them and pulls out the plain text, the page count, and any tables it finds. The two documents I want are docs/exec-summary.pdf and docs/nonexistent-does-not-exist.pdf. If one of them can't be analysed I'd much rather that document ended up in a quarantine file than have the whole run stop. Write the successful extractions out as JSON.
```

**Precondition:** confirm the second key is genuinely absent under the granted
prefix before running. The brief confirms only `docs/exec-summary.pdf` exists
and says nothing about what else may sit under that prefix — so the miss is
named `docs/nonexistent-does-not-exist.pdf` rather than a plausible-sounding
sibling. If the second key were to resolve, g07 would complete clean and the
quarantine arm would be lost **silently**, which is the worst failure mode for
this graph. Round 2 got its quarantine arm from a nonexistent object the same
way.

**Expected topology:** `csv` source (invented, two rows, a single relative-key
column such as `document_key`) → `aws_textract_document_analysis`
(`profile: acceptance-docs`, `key_field: document_key`,
`feature_types: [TABLES]` at minimum, `text_field`, `page_count_field`) → `json`
sink, with a quarantine sink on the transform's `on_error` route.

Only `docs/exec-summary.pdf` exists (confirmed in the brief).
`docs/quarterly-review.pdf` is the deliberate miss.

**Expected outcome:** `completed_with_failures` — healthy. Diagnostics: one row
analysed with non-empty `text_field` and a plausible `page_count_field`; one row
failing with a submit/not-found `reason` code and attributed to the quarantine
sink. A `bucket_region_unverified` reason on **either** row means the profile
binding did not take effect — that is a reopen of `elspeth-cd0f6a6cd9`.

**Two extra assertions, both falsifiable:**

1. **The ADR's custody NFR** — zero persisted call records containing the bucket
   literal. ADR-036 states this must fail before the change and pass after it.
   Check the Landscape call records for the run, not just the run API.
2. **The authoring surface** — the knob schema presented to the planner contains
   **none** of `TEXTRACT_PRIVATE_BINDING_OPTION_NAMES`
   (`src/elspeth/contracts/aws_textract.py:31`): `bucket`, `bucket_field`,
   `key_prefix`, `region`, `region_name`, `auth_mode`, `endpoint`,
   `endpoint_url`, and the credential names. The legal author set is
   `TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES` (`:9`) plus `profile`.

> **Wording caution.** The private set is **wider than bucket alone** — `region`
> and `key_prefix` are private too. This intent therefore names no bucket, no
> region, no prefix, and no location of any kind; it names the profile alias and
> two relative keys, which is the entire legal vocabulary. Do not "help" the
> Composer by adding location detail.
>
> **Known stale prose.** The engine-side plugin listing still advertises
> `bucket_field: document_bucket` in its `example_use`, and a composer hint
> still describes per-row `bucket_field` mode. Those are correct for CLI/YAML
> authoring, which keeps `bucket_field` fully supported. If either string
> reaches the **web** planner past the projection, that leak is itself the
> finding — the ADR's decision 3 flips the projection to a positive allowlist
> precisely so the bug class is inexpressible rather than merely discouraged.

### g07b — alias-as-bucket trap probe

Send as a **second turn in the same session**, after g07 has composed.

```
Actually, can you point that Textract step straight at the acceptance-docs bucket instead, and add a bucket column to each row so it's explicit which bucket the document came from?
```

**Three outcomes, only one of which is a reopen:**

| Outcome | Verdict |
|---|---|
| Composer refuses with an actionable message explaining the location is operator-bound | **Pass** — the intended posture |
| Composer quietly keeps the profile-first authoring and ignores the bucket request | **Pass** — weaker, but the bug class stayed inexpressible; note the silence as a UX observation |
| `bucket` or `bucket_field` reaches `safe_options` and is rejected late (or worse, accepted) | **Reopen `elspeth-cd0f6a6cd9`** |

Re-run `resolve-reviews` after this correction turn — a correction can stage
further reviews.

---

## g08 — two-LLM-arm A/B via `row_union` (sample ×3)

**Purpose:** the shape that returned a raw 500 in round 2. Intermittent at 1-in-2,
so a single clean pass proves nothing.
**Re-covers:** round-2 g02b (DEFECTS, run `f6bbca45`).
**Tickets:** `elspeth-9c01c943a5` (fixed by `cbc0e99d3`) and
`elspeth-9d13900064` (open P2).

```
I want to compare two different ways of summarising customer complaints. Read a short list of complaints — complaint id and complaint text — and send each complaint to the model twice: once asking for a one-sentence summary, and once asking for a three-bullet summary. Then stack both sets of results together into one output so I can read them side by side, tagging each row with which style produced it. The file I want at the end has just the complaint id, the style tag and the summary text. Make up four complaints.
```

**Run this three times in three fresh sessions** (`g08-s1`, `g08-s2`, `g08-s3`).
Round 2 saw the 500 on the first of two attempts.

**Expected topology:** `csv` source → `gate` with `fork_to` of length 2 → two
`llm` transform arms → `row_union` → `field_mapper` (restricting to three
fields) → `csv` sink. Plus auto-wired safety controls — round 2 saw
`prompt_shield_auto_1/2` and `safety_arm_a/b`, taking the graph to 9 nodes.

**Expected outcome — two legitimate arms.** Which one you get *is* the finding,
so record it rather than treating either as a surprise:

| If `elspeth-9d13900064` is… | Terminal state |
|---|---|
| fixed | `completed`, 8 terminal rows (4 complaints × 2 arms) |
| still live | run **fails at the downstream mapper** with `PluginContractViolation` — **this is the expected observation**, not a regression |

**What to assert:**

- **No raw 500 from compose.** Any 500 without a coded envelope is a **reopen of
  `elspeth-9c01c943a5`**, regardless of how the other two samples went.
- **`elspeth-9d13900064` still live?** Round 2 failed here at run time with
  `PluginContractViolation`: the `llm` transform's `verdict_usage` /
  `verdict_model` provenance side-fields are rejected by the downstream
  fixed-schema mapper. The trailing "just the complaint id, the style tag and the
  summary text" is what produces that fixed-schema consumer — **do not soften
  it**, or the graph stops testing the ticket. If it reproduces, add a dated
  datum to the ticket; do not file a duplicate.

> Keep g08 and g10 separate. Both sit near the LLM-schema seam, but g08's union
> failure would mask g10's `SchemaConfigModeViolation` if folded into one graph.

**Gates:** Gate 1 (`invented_source` + `llm_prompt_template` per arm). **Gate 2
fires** — the fork is a marker upstream of both LLM transforms. The driver acks
automatically.

---

## g09 — four LLM nodes, ≥4 auto-wired controls

**Purpose:** the exact shape that wedged round 2 — more auto-wired controls than
the old cap of 3.
**Re-covers:** nothing; new for round 3.
**Ticket:** `elspeth-558fa5a321`, fixed by `6610fa0d4`.

```
For each research note that comes in I want four different things from the model, all from the same note: a plain-English summary, a list of the key people and organisations mentioned, a sentiment call, and one suggested follow-up question. Run all four at once against the same note rather than one after another, then bring the four answers back together into a single row per note and save the result as JSON. Make up three research notes.
```

**Expected topology:** `csv` source → `gate` with `fork_to` of length 4 → four
`llm` transform arms → `coalesce` → `json` sink, plus auto-wired controls.
Round 2 wired 2 controls per LLM arm, so expect roughly **8 controls** and a
graph around 15–18 nodes.

**Expected outcome:** every disclosure card surfaces, and the session reaches a
**validatable** state. Then `completed` with 3 terminal rows, each carrying all
four answers.

**What to assert:** the count of surfaced disclosure cards is **not silently
capped at 3**. Round 2's wedge was requirements pending forever against zero
resolvable events; the fix must show one resolvable card per staged requirement.
After `resolve-reviews` reports zero pending, `/validate` must pass.

**No graph-size ceiling applies.** The only node bound in the web layer is
`RESPONSE_PROJECTION_MAX_NODES = 1024` (`composer/redaction.py:112`), a
redaction-projection guard, and `_transform_node_count`
(`pipeline_planner.py:962`) is only ever compared against zero. A rejection
citing graph size would itself be a new defect — do not misattribute it to
`elspeth-558fa5a321`.

**Gates:** Gate 1 (heavy — expect ~5+ reviews). **Gate 2 fires** (fork marker
upstream of four LLM transforms).

### g09b — compose cancelled mid-loop

**Ticket:** `elspeth-03f5728c33` — **closed**, fixed by `3880cb814`. Confirmed
closed 2026-08-05, so this is in scope; the brief's "check status first" is
resolved.

Replay **g09's intent verbatim** in a fresh session with a deliberately short
client timeout to force deferred cancellation mid-loop:

```bash
python scripts/acceptance_battery.py api POST /api/sessions/<sid>/messages body.json g09b/compose.json --timeout 20
```

**Then, without sending any follow-up message**, call `/validate` directly. The
fix added a backstop on both `/validate` arms
(`surface_pending_interpretation_reviews(only_missing_evidence=True)`), so the
stranded `llm_prompt_template` requirements must surface from the validate call
itself.

**Assert:** `GET /interpretations?status=pending` returns a **non-empty,
resolvable** list after `/validate`. The round-2 failure signature was pending
requirements with **zero** events — a validation block against an empty review
list. Recovery must need no compose message and no data surgery.

> **Discriminate before concluding.** The fix surfaces events via
> `only_missing_evidence=True` — it acts on requirements that have *no* events.
> If the 20s cancellation lands **before** any `llm_prompt_template` requirement
> was staged, there is nothing to strand and nothing to surface, and an empty
> review list is **correct**. So: first confirm the session actually holds
> pending requirements with zero events. If it holds none, the cancel fired too
> early — re-run with a longer timeout (try 45s, then 90s) until the cancel
> lands mid-loop with requirements staged. Only an empty review list *against
> live pending requirements* is a reopen of this closed P1.

> **The round-2 live repro is gone.** The ticket names stranded session
> `e445e8e2-9ccf-48a5-8fd8-10ffdc68b5f9` as a direct-inspection target, but the
> brief's precondition 3 requires the session store to be **recreated** at
> `SESSION_SCHEMA_EPOCH` 45. Recreation destroys that session. The strand must
> therefore be rebuilt fresh as above; do not plan around inspecting the
> original.

---

## g10 — LLM enrichment into a fixed-schema mapper

**Purpose:** re-confirm the `SchemaConfigModeViolation` on a mapper carrying an
LLM-produced field.
**Re-covers:** round-2 g10 (DEFECT, run `18d98683`).
**Ticket:** `elspeth-ed2c2315d7` (open P2).

```
Read a short CSV of job adverts with an advert id and a description. For each advert, ask the model to classify the working arrangement as onsite, hybrid or remote. Then write out a CSV containing just the advert id and that classification. Invent five adverts.
```

**Expected topology:** `csv` source → `llm` transform → `field_mapper`
(restricted to two fields) → `csv` sink, plus auto-wired controls.

**Expected outcome:** round 2 failed with `SchemaConfigModeViolation` at
post-emission on the mapper carrying the LLM-produced field. If it reproduces,
add a dated datum to `elspeth-ed2c2315d7`. If the run instead reaches
`completed` with 5 rows carrying the classification, record it as **not
reproduced against a reconstructed intent** — not as a fix.

**Gates:** Gate 1 only. **No 428** — one LLM transform, ~5 known source rows,
no upstream fanout marker, well under the 100-call threshold. If a 428 *does*
appear here, the cardinality estimator failed to bound an inline CSV source,
which is worth a separate observation.

---

## g11 — `llm` source with a dynamic schema

**Purpose:** re-confirm the compose-vs-execution parity gap.
**Re-covers:** round-2 g08 (BLOCKED).
**Ticket:** `elspeth-5a372d3267` (open P2).

```
Start this one with the model itself: have it write a short product announcement for a new release, then take that announcement, split it into individual sentences, and save each sentence as its own line in a text file.
```

**Expected topology:** `llm` source → `line_explode` → `text` sink.

**Expected outcome:** round 2's failure was compose presenting a "ready" graph
that then failed execution validation on `graph_structure` — a dynamic-schema
producer against an `llm_response` requirement. Assert on **where** it fails:
compose succeeding and `/validate` rejecting is the ticket reproducing; both
succeeding means it did not reproduce against this reconstruction.

Round 2 also saw the repair correction strand the session
(`elspeth-558fa5a321`). That is now fixed and covered by g09/g09b — if the
strand recurs here, it is a **reopen**, and this is a second independent
observation of the same seam.

**Gates:** Gate 1 (`llm_prompt_template` on the source). **No 428** — the guard
only inspects `node_type == "transform"`, and this graph's LLM is a source. A
428 here would mean the guard's node filter changed.

---

## g12 — advisor END-gate contradiction, and the prompt-retention rider

**Purpose:** confirm the advisor gate still withholds completion on a
contradictory revision, and that the recovery reply does not lie about it.
**Re-covers:** round-2 `adv` (GREEN gate + DEFECT copy, run `ed16f06b`).
**Tickets:** `elspeth-2306940c70` (open P2) and `elspeth-49b467d91a`
(verifying) — this is the round's best chance to discharge the latter.

**Turn 1 — establish a correct gate:**

```
Read a small CSV of transactions with an id and an amount. Send transactions over 200 to a big_amounts file and everything else to a small_amounts file. Invent three transactions — please include one small one, one large one, and one right in the middle.
```

**Turn 2 — the contradiction:**

```
Great. Now also make sure every single transaction ends up in big_amounts no matter what the amount is, but keep the routing exactly as you've just set it up.
```

Turn 2 is self-contradictory by construction: it demands universal routing to
one sink *and* preservation of the two-way split.

**What to assert, in order:**

1. **The gate holds.** Turn 2 must **withhold completion** with a FLAG. A turn-2
   response reporting success is a gate regression — *but check first whether
   the advisor read turn 2 as a no-op rather than a conflict.* The contradiction
   rests on "keep the routing exactly as you've just set it up"; if the model
   treated that clause as "change nothing" it never saw a conflict, and the
   right move is to sharpen the wording and re-run, not to log a regression.
2. **The recovery reply is honest** (`elspeth-2306940c70`). Round 2's recovery
   falsely told the user the refused instruction was "already live". Read the
   assistant copy: any claim that the contradictory instruction is in effect
   reproduces the ticket — add a dated datum.
3. **The prompt is retained** (`elspeth-49b467d91a`). Turn 2 is a *failing*
   compose, which is the recipe the brief identifies. Confirm the typed message
   is **restored rather than discarded**. Over the API this means the turn-2
   content is recoverable — check `GET /api/sessions/{id}/messages` and the
   failing response body. If retention is only observable in the SPA's composer
   box, drive it with Playwright and say so in the report rather than inferring
   it from the API.
4. **The design still runs.** After recovery, execute and confirm the original
   routing claim was true: the large transaction to `big_amounts`, the small one
   to `small_amounts`.

> **Do not run blanket `resolve-reviews` on this graph.** The driver hardcodes
> `accepted_as_drafted`, which would accept the retained draft before assertion
> 3 has inspected it. Resolve manually here.

**Gates:** Gate 1 (manual). No 428 — no LLM node in the authored pipeline.

---

# Rider — F14 guided cold start (not a graph)

`elspeth-5904b1683a` is closed; round 2 measured 10/10 against a pre-fix 1/5.
Anything below 10/10 warrants a reopen. `guided-rider` needs one canonical
intent, replayed identically across all ten attempts:

```
Read a small CSV of customer feedback with an id and a comment, drop any row where the comment is empty, and save what's left as JSON Lines.
```

```bash
python scripts/acceptance_battery.py guided-rider 10 --intent "Read a small CSV of customer feedback with an id and a comment, drop any row where the comment is empty, and save what's left as JSON Lines."
```

Each attempt is a fresh session and operation id, `profile: live`. A client
timeout is reconciled against the server's authoritative outcome rather than
counted as a failure. Assert: 10 HTTP 200s with non-null composition state, zero
`REPAIR_EXHAUSTED`, zero 502s, zero `planner_repair_exhausted`.

Pin this exact string into the round-3 report so round 4 replays it rather than
reconstructing another one.

---

# Suggested execution order

1. **g01–g06** first — cheap, no LLM, no AWS. They establish the instance is
   healthy. A failure here is a regression that invalidates everything after it,
   so stop and diagnose rather than continuing.
2. **g07 + g07b** — Textract, needs `ELSPETH_WEB__AWS_TEXTRACT_PROFILES` live.
   Run early enough that a `profile_unavailable` finding still leaves time to
   fix the task definition.
3. **g08 ×3** — the three samples are the round's most schedule-sensitive item
   because the defect is intermittent. Do not compress to fewer.
4. **g09, then g09b** — g09b replays g09's intent, so compose g09 successfully
   first to know the intent is sound before deliberately cancelling it.
5. **g10, g11, g12** — the three open-P2 re-confirmations.
6. **F14 rider** — last, and independent of everything above.

The stack's `CleanupDeadline` is `2026-08-06T00:00:00Z`. Capture ticket evidence
(event listings, node requirement JSON, diagnostics) **while the instance is
up** — round 2 learned that a live repro expires.
