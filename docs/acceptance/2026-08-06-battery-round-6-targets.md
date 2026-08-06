# Round 6 targets — weather after round 5, and the faults to hit

Date: 2026-08-06. Author: `claude-r5-battery`. Source of record:
`2026-08-06-battery-round-5-report.md` (all numbers trace there and to
`ops-local/acceptance/r5-preserve/`). This document seeds the round-6 brief:
the weather is the baseline to beat; the fault list is the fix wave's
worklist, in priority order, each with the criterion that will be applied
live.

## Weather report — round 5 (arm A primary; B/C noted where they differ)

| | Graph | Round 5 |
|---|---|---|
| ☀️ | g01 linear multi-transform ×3 | `completed` 3/3 — 4/4 rows, 0 rejected (was ⛈ empty-runs in R4) |
| ☀️ | g02 gate + routing + poisoned row ×4 | `completed_with_failures` 4/1/1 (designed) in all four samples (A×3 + C×1) |
| ⛅ | g03 fork / coalesce | A: compose-422 at the 270s wall; B: `completed` at 840s — first authoring call lands t=413s (`elspeth-09c91778f5`) |
| ⛅ | g04 json + explode | A: wall death *inside the review byte-match loop*; B: `completed` in 232s — under the old ceiling once the loop was survivable (`elspeth-9d59c33480`) |
| ☀️ | g05 text → text | `completed` 6/6 — composer prospectively routed around `value_transform` per the corrected hints (was ⛈ R3, designed-decline R4) |
| ☀️ | g06 sink variety | `completed` |
| ☀️ | g07 Textract profile-first | `completed_with_failures` 1/1 (designed); ADR-036 shape intact |
| ⛅ | g08 row_union A/B ×3 | 1 `completed`, 2 validate-FAILED on the `row_union` edge with `suggestion: null` (`elspeth-41bcaa882e`) — NOT extra_forbidden |
| ⛅ | g09 four LLM nodes | A: `completed` 3/3 rows, settled t=265.7s — **4.3s inside the parity wall**; C: compose-422 at the stock 240s wall |
| ☀️ | g10 LLM → fixed mapper | `completed` |
| ⛈ | g11 llm source | A: wall with invalid partial; B: `completed_with_failures` **0/6 rows, 0 artifacts** — auto-wired shield killed every row (`elspeth-b19dfe41fb`); the llm-*source* mechanism itself is still unsampled |
| ☀️ | guided rider ×10 | 10/10 successful *starts* (one call deep — no completion claim) |

Net vs round 4: two graphs left the storm column (g01, g05), g11 stayed in
it for a **new** reason, and both remaining ⛅ rows now have *named,
filed mechanisms* rather than "the wall".

## The faults to hit

Ordered by blast radius on the next battery. Every LLM-mediated fix gets the
two-level rule: Level 1 deterministic and off-stack before any compose is
spent on Level 2.

### P1 — the fix wave

| # | Ticket | Fault | Level-1 criterion (deterministic) | Level-2 criterion (live, ×3 where stochastic) |
|---|---|---|---|---|
| 1 | `elspeth-41bcaa882e` (**fixing**) | `row_union` publishes no output guarantees; downstream `required_fields` failure carries `suggestion: null` and a remediation naming an option the node never set | The g08-s2 edge failure carries a non-null, *actionable* suggestion naming the option that actually exists (`schema.required_fields`) | g08 clean **×3** — this is also `elspeth-902fc354b2`'s unblock; that ticket closes only through here |
| 2 | `elspeth-155947ca47` | Interpretation-review resolution re-persists `is_valid` from the narrower authoring validator, flipping false→true while a `graph_structure` violation is live | Deterministic repro from the g08 shape: resolve a review on an invalid state; persisted `is_valid` must stay false (or re-derive from the full preflight) | Re-drive g08-s2's sequence; `/state` and `/validate` must agree at every version |
| 3 | `elspeth-9d59c33480` | `request_interpretation_review` demands a byte-identical `llm_draft` the composer cannot re-emit (decoded-newline mismatch) — deterministic 4-attempt loop, settled only by the driver's out-of-band `/resolve` | Unit: the tool accepts a server-side draft reference (or normalises before compare); the byte-match path is gone | g04 composes and surfaces its review **in-band**, no driver rescue, inside the parity wall |
| 4 | `elspeth-b19dfe41fb` | Auto-wired prompt shield on a declared-`int` field passes `/validate` then kills 100% of rows | `/validate` rejects (or the auto-wire refuses) a shield targeting a non-string field under `mode: fixed` — statically knowable | g11 (or any shielded graph) runs with rows surviving the shield it was given |

### P2 — behind the wave

| # | Ticket | Fault | Criterion |
|---|---|---|---|
| 5 | `elspeth-3d391accce` | `graph_structure` violation reaches only assistant free text at compose settle; structured `validation_errors` omits it | Machine consumers see the violation in the structured field; g08-s2's settle payload is the fixture |
| 6 | `elspeth-09c91778f5` | Shipped envelope cannot fund the shipped corpus (g03 t=413s vs 270 ceiling; g09 marginal at 4.3s; `b5047ef69`'s "ZERO composes flip at 240" now falsified by g09) | **Product decision, not a patch**: raise the package ALB/ceiling legs together, or shrink the corpus claim. The three-place change is documented in the round-5 brief's arm-B correction |

### Cheap wins (buy wall-clock directly)

| # | Item | Evidence | Shape |
|---|---|---|---|
| 7 | `elspeth-obs-340d35c55c` — `type_coerce.conversions` shape undiscoverable without `get_plugin_schema` | 4/4 blind composes spent a repair turn on list-vs-dict; the 1 schema-first compose got it right; ~10% of a 240s envelope per occurrence | Carry the shape in `composer_hints` or the discovery digest |
| 8 | `elspeth-obs-62c0f57b6d` — two discard surfaces report 0 and 6 for the same run | `/diagnostics` discards `[]`/0 vs `discard_summary.total` 6; reason on a third surface | Resolve with `elspeth-47fa7c01eb` (its readable-but-uninformative sibling) — unify or rename the vocabularies |

### Carried into round 6 (not faults; owed verification)

- **g01 + g02 second stochastic pass** — both CONFIRMED 3/3 and 4/4 this
  round; the brief's rule holds them open until a second pass.
- **g11 mechanism sampling** — settle the corpus-vs-brief mapping
  (`5a372d3267` vs `39118dd24f`) first; then a compose that actually
  exercises an `llm` source. Two rounds have now authored around it.
- **Advisor END gate** — still zero `phase=end` events in any round; measure
  via the `completion_gates` envelope in the round-6 cost pass.
- **Driver**: capture tool-*result* messages (closes the last inferential
  step in the `41bcaa882e` chain); the `/state`+`/state/yaml`+`/messages`
  captures are in place since round 5.
- **`elspeth-49b467d91a`** (frontend DOM) is now Playwright-reachable over
  real TLS on `elspeth.aws.foundryside.dev` — first browser-based sample
  possible.
