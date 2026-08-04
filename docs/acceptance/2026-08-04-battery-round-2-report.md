# Acceptance battery round 2 — report

Date: 2026-08-04. Operator surface: `https://elspeth.aws.foundryside.dev`.
Deployed release `173a81cbb` (wave-3 tip), task definition
`a-fa1b99c60192978b10f7-web:9`, image digest
`sha256:c1b81ff4073fdc9a94aca99e27d4ab4dd9a4ed6dfb2199a33931080830c37f7f`,
session store recreated at `SESSION_SCHEMA_EPOCH` 44, Landscape epoch 30.
Driver: external HTTPS traffic under a self-registered local account
(`battery-r2-aface8c5`); every verdict is read from the run API / Landscape
audit trail, never from output-file mtimes.

## Per-graph results

| # | Graph (coverage) | Verdict | Run id | Evidence |
|---|---|---|---|---|
| g01 | linear × 3 `field_mapper` (inline csv) | **GREEN** | `1ab41ff4` | completed; 4 source rows, 4 tokens terminal, 4 succeeded, 0 failed |
| g02b | row_union A/B, two LLM arms (`csv` → 2 × llm → row_union → mapper) | **DEFECTS** | `f6bbca45` | compose reached 9 nodes incl. auto-wired `prompt_shield_auto_1/2` + `safety_arm_a/b`; LLM-fanout guard fired (428 → ack token → 202); run failed `PluginContractViolation` — the llm transform's `verdict_usage`/`verdict_model` provenance fields are rejected by the downstream fixed-schema mapper (`elspeth-9d13900064`). First attempt of this graph returned a raw 500 (`elspeth-9c01c943a5`) |
| g03b | `aws_s3` manifest → Textract → extracted/quarantine | **GREEN (as designed)** | `ea9ec481` | completed_with_failures; real PDF analysed (deployment-derived region, bucket-region check passed — D6 datum), nonexistent object `submit_failed` → quarantine sink. Earlier attempt `e41d0e6b` exercised the other enforcement arm (`bucket_region_unverified`, all rows quarantined) |
| g04 | gate with named routes + poisoned row + `on_error` | **GREEN** | `43743e6c` | 91/77 → `premium`, 44 → `standard`, `not-a-number` → source-validation failure → `bad_rows` quarantine; siblings unaffected |
| g05 | fork → 2 branches → coalesce | **GREEN** | `9987875a` | completed; 24 node states across 3 rows; all 3 merged rows reached `combined` |
| g06 | `json` source | **GREEN** | `d76e1ffd` | completed |
| g07 | `text` source | **GREEN** | `8868e411` | completed |
| g08 | `llm` source | **BLOCKED** | — | compose presented "ready" but execution validation failed `graph_structure` (dynamic-schema producer vs `llm_response` requirement — `elspeth-5a372d3267`); the repair correction then stranded the session: node interpretation requirements pending forever with zero resolvable events (`elspeth-558fa5a321`) |
| g09 | sink variety (csv + jsonl + text) | **GREEN** | `dd70a319` | completed; three sink plugins from one source |
| g10 | LLM-enrichment linear | **DEFECT** | `18d98683` | `SchemaConfigModeViolation` at post-emission on the mapper carrying an llm-produced field (`elspeth-ed2c2315d7`) |
| adv | advisor END-gate probe (extra) | **GREEN (gate) + DEFECT (copy)** | `ed16f06b` | contradictory revision correctly WITHHELD completion (FLAG); recovery restored the gate design and ran correctly — 250 → `big_amounts`, 40 → `small_amounts`, 900 → `big_amounts`. Recovery reply falsely told the user the refused instruction was "already live" (`elspeth-2306940c70`) |

Coverage achieved: sources `csv` / `json` / `text` / `llm` / `aws_s3`; sinks
`csv` / `json` (jsonl) / `text`; topologies linear, fork+coalesce, gate with
named routing, row_union A/B, and Textract document analysis; `on_error`
routing exercised by a poisoned row and by two transform-failure paths.

## Sampling riders

- **F14 post-fix (`elspeth-5904b1683a`) — 10/10.** Ten fresh plain-guided cold
  starts (`POST /guided/start`, `profile: live`, new session and operation id
  each, identical canonical intent) all returned HTTP 200 with a non-null
  composition state. Zero `REPAIR_EXHAUSTED`, zero 502s, zero
  `planner_repair_exhausted`. Pre-fix live rate on `67e2a1661` was 1/5.
- **Retry exhaustion (`elspeth-454892147c`) — UNSAMPLED.** Induced provider
  failures inside a retry-enabled pipeline are not constructible from outside
  the deployment, and no natural provider failure occurred across the 12 runs.
  Adjacent (not substitute) evidence: `on_error` routing to named quarantine
  sinks is demonstrably live in runs `43743e6c`, `ea9ec481`, `e41d0e6b`.
- **Advisor END gate FLAG → repair → CLEAN — OBSERVED.** See the `adv` row:
  the FLAG, the repair turn, the CLEAN completion, and an execution proving
  the routing claim was true.
- **Prompt retention (`elspeth-49b467d91a`) — UNSAMPLED.** The failure it
  recovers from never occurred (a consequence of the F14 fix landing).

## Defects filed (label `battery-2026-08-04`)

| Id | P | Summary |
|---|---|---|
| `elspeth-9c01c943a5` | P1 | Freeform compose 500s raw: `pipeline_decision` event draft mismatches the node review requirement; no coded envelope. Intermittent (1/2) |
| `elspeth-558fa5a321` | P1 | Compose correction strands the session: requirements pending forever while events auto-opt-out — same event-vs-requirement seam as above |
| `elspeth-9d13900064` | P2 | LLM provenance side-fields (`*_usage`, `*_model`) break downstream fixed-schema consumers at runtime |
| `elspeth-ed2c2315d7` | P2 | `SchemaConfigModeViolation` on a mapper carrying an llm-produced field |
| `elspeth-5a372d3267` | P2 | Compose presents graphs that fail execution `graph_structure` validation (compose-vs-execution parity gap) |
| `elspeth-cd0f6a6cd9` | P2 | Composer writes the S3 profile **alias** as a literal bucket value for Textract rows |
| `elspeth-2306940c70` | P2 | Post-advisor-FLAG recovery reply tells the user their refused instruction is live |

Theme: five of seven sit on one seam — **the composer's authoring-time model
of LLM-node schemas and interpretation reviews diverges from what the engine
enforces at run time**. Compose says ready, validate or the executor says no.
The failures are all loud and fail-closed (no silent bad data), and the
diagnostics were sufficient to root-cause each from the run API alone.

## Redeploy verification

- `ELSPETH_WEB__AWS_S3_SOURCE_PROFILES` live: `aws_s3` present in the compiled
  catalog and the `acceptance-docs` profile both composes and runs (g03b).
- `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1` live in the task definition, so a
  future F14-class exhaustion is diagnosable from CloudWatch by
  component + code without validator messages leaving the process.

## Close recommendations (operator sign-off required)

- `elspeth-5904b1683a` (F14) — **recommend close**: 10/10 on the deployed
  wave-3 tip, `fix_verification` satisfied.
- `elspeth-1033d97b6c` (D6 Textract region) — **recommend close**: both
  enforcement arms observed live.
- `elspeth-454892147c` (retry exhaustion) — **keep open**: unsampled this
  round.
- `elspeth-49b467d91a` (prompt retention) — **keep in verifying**: unsampled
  live; unit coverage green.

Stochastic items are not closed on a single pass.
