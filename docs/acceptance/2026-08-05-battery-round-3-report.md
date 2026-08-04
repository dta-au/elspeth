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

The redeploy discharged its purpose: **ADR-036 is confirmed live, both halves.**

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
| g01 linear multi-transform | (see note) | compose 422 under load; **197s / 200 serial** | Methodology, not a defect |
| g02 gate + named routing + poisoned row | `bb683d20-bf9a-4c47-bc91-d9b0bb7bbcbf` | `completed_with_failures` 4/1/1 | **PASS** — designed outcome, `on_error` route exercised |
| g03 fork / coalesce | `b944ac9c-52a1-421b-8ed2-93b8b982cfdf` | `completed` | **PASS** |
| g04 `json` source + explode | `57cc5039-0567-4edf-a4df-dee0c912c15e` | `completed` 6/0/0 | **PASS** |
| g05 `text` source + `text` sink | `adf0b6c6-bdcb-4e29-ba23-b953bae5366c` | `failed` | **NEW** `elspeth-cfcd333f83` |
| g06 sink variety | `5c9964cb-974c-4f24-9198-45e087d4e47b` | `completed` | **PASS** |
| g07 profile-first Textract | `67d40936-2261-4b30-9b43-7058f7927e53` | `completed_with_failures` 1/1 | **PASS** — ADR-036 confirmed |
| g07 (first attempt) | `c69b6ab6-e462-46d8-bd9b-2c8a811bb02f` | failed at **source** | **NEW P1** `elspeth-47fa7c01eb` |
| g08 two-LLM-arm `row_union` A/B | `dc689cfe-9f4b-47ba-bd04-abd04309debc` | `failed` | Compose fix holds; **NEW** `elspeth-902fc354b2` |
| g09 four LLM nodes | — | compose 422 under load | Not exercised; see below |
| g10 LLM → fixed-schema mapper | `37caf1ee-00c2-48af-b3ca-2f5d39963154` | `completed` | **PASS** — `elspeth-ed2c2315d7` confirmed |
| g11 `llm` source, dynamic schema | `a4f534df-2319-46be-95e0-453028375c13` | `failed` | Parity gap closed; **NEW P1** `elspeth-39118dd24f` |

## Fixes confirmed live

- **`elspeth-cd0f6a6cd9` / ADR-036 — both halves.** *Authoring:* given prose
  naming no bucket, region or prefix, the Composer authored
  `profile: acceptance-docs`, `key_field: document_path`, and no location
  vocabulary at all — decision 3's allowlist projection holding on a real
  graph. *Runtime:* the real document analysed, the deliberately-absent key
  failed with `service_error` and took the `on_error` route. The
  discriminating negative is that the failure was **not**
  `bucket_region_unverified`, which would have meant the alias was still being
  HeadBucket-ed as a literal. *Custody NFR:* zero occurrences of the bucket
  literal or org prefix across run status, diagnostics, outputs and results.
  Scope limit stated on the ticket: the ADR's assertion is about Landscape
  call records, which could not be queried remotely.
- **`elspeth-ed2c2315d7`** — g10 `completed`. This exact shape failed at
  runtime in round 2.
- **`elspeth-5a372d3267`** — g11's `/validate` returned `is_valid=true`. The
  round-2 defect was compose presenting a graph that then failed execution
  `graph_structure` validation; that parity gap is closed. (The run failed for
  an unrelated reason — see `elspeth-39118dd24f`.)
- **`elspeth-9c01c943a5`** — g08 composed and validated with no raw 500. The
  round-2 defect was an uncoded 500 at compose; it did not recur.
- **`elspeth-03f5728c33`** — observed working incidentally: on g01, `/validate`
  named two pending interpretation reviews that the
  `?status=pending` listing reported as empty. That is precisely the backstop,
  on a strand that arose naturally rather than one constructed for the test.
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

**Do not parallelise composes on this deployment.** Six concurrent composes
against a single 1 vCPU / 2 GB task pushed two of them past the 270s
wall-clock budget into `convergence_wall_clock_timeout` 422s. The serial
control settles it: g01 alone composes in **197s / HTTP 200**; the same graph
under load returned 422 at 271s. No defect was filed.

The residual datum is the margin, and it is thin: the *simplest* graph in the
corpus consumes 73% of the wall-clock budget on a completely idle instance.
Observed serial-ish compose times ranged 43s–238s with no clear relation to
graph complexity — g02 (gate, named routing, poisoned row) took 43s while g01
(linear) took 197s. A per-task reasoning-budget change was landing as this
round ran and is expected to move these numbers; round 4 should re-measure
rather than treat these as a baseline.

**The engine's error text can point the wrong way.** `elspeth-39118dd24f`
reports "This indicates an upstream transform/source schema bug" for a row
whose upstream behaved correctly. Two of this round's five findings needed the
composed graph JSON, not the error message, to locate the real cause.

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

## Reporting posture

Recommendations only — **no ticket was closed by this round.** Operator
sign-off closes, and the close then carries
`close_commit release/0.7.2@<tip>`. AWS mutations are recorded in the ledger
in `2026-08-03-r3-rca-remediation-tracker.md`.
