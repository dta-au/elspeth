# Battery round 8 — report

Pin **`230fd9dfd`**, 23 commits past round 7's `51d3d26c9`. Image roll onto the
round-7 Sydney cold install (run `700e19d5-7894-4087-9a04-25aca8047b26`,
TD `:10`). Plan and coverage declaration: `2026-08-09-battery-round-8-plan.md`.
AWS mutations: `ops-local/acceptance/r8-aws-ledger.md`.

## Headline

**Zero outcome regressions across the corpus, and the two divergences are both
improvements.** 15 of 19 arm-A graphs are outcome-equivalent to round 7; g03-s3
went `failed → completed` and g07 went `failed → completed_with_failures`. The
remaining two have no round-7 counterpart to compare against.

Nine tickets gained live evidence, three of them for the first time ever.
Three new defects were filed. One demo-blocker reproduced and must not close.

| | round 7 | round 8 |
|---|---|---|
| arm A graphs driven | 19/19 | 19/19 |
| wall deaths | 0 | **0** |
| max wall (server wall 840s) | 377s | 318s |
| pytest at the pin | 2 failed (tracked) | **0 failed** |

## What changed about how this round was read

Round 8's per-graph table reports **outcome equivalence**, not authored-shape
equality. The composer re-derives a pipeline from prose every run, so a
different construction reaching the same behaviour is evidence that real
composition is happening — not a regression. Ten of nineteen graphs authored a
different shape; eight of those ten were outcome-equivalent. Shape difference is
an attribution lever for explaining an outcome change, and nothing more.

## Arm A — the 19-graph corpus

Byte-identical to round 7 (same graphs, sampling, timeouts), so any difference
is attributable to the 23 commits.

| Graph | rc | Wall | Run status | Tokens | Outcomes | Artifacts | Shape vs R7 | Outcome vs R7 |
|---|---|---|---|---|---|---|---|---|
| g01-s1 | 0 | 135s | completed | 4 | success:4 | 1 | same | equivalent |
| g01-s2 | 0 | 169s | completed | 4 | success:4 | 1 | differs | equivalent |
| g01-s3 | 0 | 169s | completed | 4 | success:4 | 1 | same | equivalent |
| g02-s1 | 0 | 78s | completed_with_failures | 5 | failure:1, success:4 | 3 | differs | equivalent |
| g02-s2 | 0 | 73s | completed_with_failures | 5 | failure:1, success:4 | 3 | same | equivalent |
| g02-s3 | 0 | 79s | completed_with_failures | 5 | failure:1, success:4 | 3 | same | equivalent |
| g03-s1 | 1 | 131s | never ran (validate rejected) | — | — | — | differs | r7 also never ran |
| g03-s2 | 0 | 242s | **failed** | 3 | failure:1, transient:1, open:1 | 0 | differs | r7 never ran |
| g03-s3 | 0 | 121s | completed | 12 | success:9, transient:3 | 1 | differs | **DIVERGENT (failed → completed)** |
| g04 | 0 | 142s | completed | 9 | success:6, transient:3 | 1 | same | equivalent |
| g05 | 0 | 215s | completed | 6 | success:6 | 1 | differs | equivalent* |
| g06 | 0 | 193s | completed | 20 | success:15, transient:5 | 3 | same | equivalent |
| g07 | 0 | 94s | completed_with_failures | 2 | failure:1, success:1 | 2 | same | **DIVERGENT (failed → completed_with_failures)** |
| g08-s1 | 0 | 318s | completed | 12 | success:8, transient:4 | 1 | same | equivalent |
| g08-s2 | 0 | 158s | completed | 12 | success:8, transient:4 | 1 | differs | equivalent |
| g08-s3 | 0 | 224s | completed | 12 | success:8, transient:4 | 1 | same | equivalent |
| g09 | 0 | 295s | completed | 3 | success:3 | 1 | differs | equivalent |
| g10 | 0 | 135s | completed | 5 | success:5 | 1 | differs | equivalent |
| g11 | 0 | 117s | completed | 9 | success:8, transient:1 | 1 | differs | equivalent* |

`equivalent*` = same status and artifact count, different intermediate token
count — a differently-built graph carrying different intermediate tokens to the
same result.

## Arm C — new probes

Built because arm A structurally cannot reach two tickets.

| Graph | Wall | Status | Verdict |
|---|---|---|---|
| g13-s1 | 151s | completed_with_failures | **PASS** — `branch_lost:branch_risk` recorded |
| g13-s2 | 179s | completed_with_failures | **PASS** — `branch_lost:branch_risk` recorded |
| g13-s3 | 288s | completed | **PASS** |
| g14-s1 | 157s | completed | sink authored `flexible`, not locked |
| g14-s2 | 111s | completed | sink authored `flexible`, not locked |
| g15-s1 | 144s | completed | sink authored **`fixed`** + `field_mapper` to drop extras |

## Arm B — blob custody

Arm A cannot reach Lane A at all: no corpus intent uploads a blob.

| Probe | Ticket | Result |
|---|---|---|
| B3 | `elspeth-b3feba9a7c` | **PASS** — `409: Blob … is linked to active run … and cannot be deleted` |
| B2 | `elspeth-3d1d1fcb6c` | **PASS** — 8 concurrent requests, exactly 1 delete, legal serial history, no 5xx, no torn read |
| B1 | contract check | **PASS** — an idle blob deletes cleanly; the in-flight guard correctly does not over-apply |

## Ticket verdicts

**Verified — recommend close**

| Ticket | Evidence |
|---|---|
| `elspeth-74b795208f` | **3/3, first live evidence ever.** See below |
| `elspeth-15c72686f2` | Build-time rejection fires correctly. See below |
| `elspeth-b3feba9a7c` | B3: 409 during an active run, precise message |
| `elspeth-3d1d1fcb6c` | B2: concurrent serialization holds |
| `elspeth-62a5aa4da8` | `230fd9dfd` fixes it; full pytest at the pin is clean |
| `elspeth-aed3b69cf0` | g02 2/3 exercised, both PASS, shapes match r7 |
| `elspeth-3664e213c4` | g01 3/3, second consecutive clean round |
| `elspeth-902fc354b2` / `41bcaa882e` | g08 3/3, no regression on already-closed tickets |
| `elspeth-d1602e4b90` | g05 clean, second sample |

**Do NOT close**

`elspeth-85f3cc3022` — reproducing. g03-s1 authored a coalesce → `field_mapper`
edge, `/validate` rejected it (`producer emits 'Any'` vs `consumer requires
'str'`), and the compose loop **terminated after four tool calls with no repair
attempt**. Static parity passes again; the repair half does not.

Round 8 sharpens the mechanism. Compare where the rejection surfaced:

| | rejection surfaced at | composer response |
|---|---|---|
| g15-s1 | `set_pipeline` — **inside** the compose loop | repaired: `rejected → applied → patch_node_options`, final graph valid |
| g03-s1 | `/validate` — **after** the loop ended | terminated, no repair |

The composer repairs what it can see during the loop and terminates blind to
what only the validate endpoint catches. The actionable form of this ticket is
therefore: the compose loop must run the same validation `/validate` runs before
declaring completion.

**Still NOT exercised**

- `elspeth-9595abb7b0` — zero diversions again, second round running.
- `elspeth-045ad8de9d` — the bootstrap provisioner **succeeded** (56s), so its
  stderr-surfacing arm never fired. Code-verified only, as declared in the plan.
- `elspeth-8363555f05` — frontend-only; no HTTP driver reaches it.
- guided cluster — arm D dropped. **UNMEASURED**, not "no regression".

## New defects

| Ticket | Pri | Summary |
|---|---|---|
| `elspeth-220a623eb6` | P2 | `type_coerce` emits field metadata inconsistent with its own declaration; g03-s2 passed both gates then died on row 1 |
| `elspeth-88a4db09f9` | P2 | Freeform composer never length-validates output names — `validate_composer_output_name` is wired only to guided prefill |
| `elspeth-950486711d` | P3 | Outcome-level strictness language maps to `mode: flexible`, not `fixed` |

## The two tickets arm A could not have reached

**`elspeth-74b795208f` — unreachable by any existing graph.** The
`gate_routed_to_sink` branch-loss row only writes when `branch_name is not None`
AND the branch belongs to a coalesce (`processor.py:3051`). g03 and g09 fork and
coalesce but hold no gate; g12 gates but never forks. No corpus graph has both,
so the ticket was structurally unreachable and would have stayed so however many
rounds ran. **g13** supplies `fork → gate-routes-a-branch → coalesce`, and all
three samples recorded:

```
failure_reason:    'branch_lost:branch_risk'
branches_arrived:  ['branch_ref']
expected_branches: ['branch_ref', 'branch_risk']
```

The barrier was notified, the reason is categorical, no forked token was left in
flight, and **the audit write for a handled condition did not kill the run** —
which was the ticket's actual symptom. Not proven: the persisted `reason` column
value, since no API route exposes `coalesce_branch_losses`.

**`elspeth-15c72686f2` — the composer will not author the shape.** The ticket
needs `llm source → line_explode → LOCKED sink`. g11 authors the topology every
time and an unlocked sink every time (`flexible` r7, `observed` r8); an unlocked
sink cannot exhibit the defect in either direction. Verified instead by taking
g11's **own** composer-authored YAML from `/state/yaml` and changing exactly one
field (`observed` → `fixed`), then running `elspeth validate`:

```
Consumer (text) input is locked (mode: fixed) and accepts: ['sentence']
Producer (line_explode) guarantees: ['llm_response_model', 'llm_response_usage', 'sentence']
Extra fields rejected: ['llm_response_model', 'llm_response_usage']
```

The walk now sees past the forwarding transform to the llm source's guarantees —
the precise extras, at the precise hop `bb29555e5` fixed. Probe preserved at
`ops-local/acceptance/r8-15c72686f2-probe/settings.yaml`.

## Corpus coverage findings

Three assumed coverages do not hold, and they explain most of this round's
"not exercised" verdicts:

1. **g09 authors zero branch tokens, in both rounds.** Its intent asks for four
   parallel calls into a coalesce; the composer writes a single `llm` node. The
   fan-out/coalesce path has never been tested by g09.
2. **g11 authors an unlocked sink, in both rounds.** No locked-consumer defect
   is reachable through it.
3. **No corpus intent uploads a blob**, so Lane A never executes during a
   compose round.

The corpus tests what the composer chooses to author, which is not the same as
what the intent asks for. Any ticket whose failure mode needs a specific
topology or contract mode needs a purpose-built graph or a YAML probe.

## Instrument errors made this round

Recorded so they are not repeated.

1. **Three pytest failures were CPU starvation, not regressions.** The suite ran
   with 12 workers against a concurrent docker build; two of the three are
   contention/timing tests. All pass serially on an idle box.
2. **Editing a shell script while it is running corrupts it.** `r8_battery.sh`
   was edited mid-arm to add g15; bash re-reads scripts incrementally, so
   g14-s2 ran twice and the loop died on `syntax error near unexpected token
   'done'`. Data intact, one sample wasted.
3. **`analyse_g13.py`'s first draft would have inverted its own verdict.** It
   invented the terminal vocabulary from memory (`{SUCCESS, FAILED, …}`; the
   real `TerminalOutcome` is `{success, failure, transient}`) and keyed on a
   `terminal_path`/`sink_name` that tokens do not carry. Both fail silently.
   Caught by dry-running against preserved round-7 captures before trusting it.
4. **A status-based oracle read the g13 result backwards.** `completed_with_failures`
   is the DESIGNED outcome when a gate-routed branch makes its sibling's merge
   unsatisfiable, so the first verdict logic scored FAIL on exactly the two
   samples carrying the positive evidence.
5. **Arm B's B1 asserted a contract the code never promised**, and reported a
   DEFECT for it. Deletion is guarded only by three *in-flight* conditions; a
   committed composition does not pin a blob forever. B3 is the probe that
   actually reaches the guard — and it only worked once reordered to run before
   B1 deleted the blob the run depended on.

## Cost

Arm A 19 + arm C 6 (one duplicate) + arm B 2 + preflight 3 = 30 compose
sessions. At round 7's measured USD 0.3391/session ≈ **USD 10.2**.

The three g13 preflights cost roughly one arm-C sample and prevented a
three-sample arm from returning `NOT-EXERCISED`; they also produced two of the
three new defect filings.
