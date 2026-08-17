# Composer path-quality battery

Spec: `docs/superpowers/specs/2026-08-13-composer-battery-design.md` (rev 4).
Plan: `docs/superpowers/plans/2026-08-17-composer-battery.md`.

The battery fires a fixed operator-voice corpus (`corpus.md`, 18 stratified
cases + canary) at the local live composer, captures every run to
`runs/<round>/<case>/<n>/`, and scores **offline** against each case's
pre-registered floor (`scenarios/<case>/scenario.json`) with the §3
deviation taxonomy. Nothing here executes a pipeline; nothing here registers
an account.

## Prerequisites

- `source .venv/bin/activate` in the main checkout.
- `~/.elspeth-battery/credentials.json` (mode 600):
  `{"username": "<account>", "password": "…"}` — whichever composer account
  the operator has provisioned on the substrate (`elspeth-web` local auth);
  the driver logs in and **never registers**, so the account must already
  exist. Any other mode than 600 is a config refusal (exit 64). The sibling
  harnesses' env names `ELSPETH_EVAL_USER` / `ELSPETH_EVAL_PASS` /
  `ELSPETH_EVAL_BASE_URL` work for one-off runs; with no file and no
  `ELSPETH_EVAL_PASS`, the driver prompts, which means it cannot run
  unattended.
- **Redeploy first if `src/elspeth` has moved since the service last
  started.** The battery measures the *deployed* composer, and
  `/api/system/status` is read at boot, so a long-lived process reports a
  stale `frontend_build` into the binding identity. Check
  `systemctl show -p ActiveEnterTimestamp --value elspeth-web.service`
  against `git log -1 -- src/elspeth`; if it is behind:
  `(cd src/elspeth/web/frontend && npm run build)` then
  `sudo systemctl restart elspeth-web.service`, poll `/api/ready` until 200,
  and confirm no `Scheduled restart job` lines in
  `journalctl -u elspeth-web.service` (a restart can reveal a stale
  Landscape schema epoch, and `systemctl is-active` lies under a
  crash-loop).
- `deploy/elspeth-web.env` present (advisor model and the two turn budgets
  are read from it into the binding identity).
- The substrate is healthy: `curl -s https://elspeth.foundryside.dev/api/system/status | jq .composer_available` → `true`.

## Commands

| Step | Command |
| --- | --- |
| Unit gate (offline) | `pytest tests/unit/evals/composer_battery -q` |
| Dry-run the corpus through the classifier | `python -c 'from evals.lib.battery_corpus import load_corpus; from elspeth.web.composer.no_tool_policy import classify_pipeline_mutation_intent as c; [print(n, c(k.prompt).name) for n,k in load_corpus()[1].items()]'` |
| Fire a round (canary N=10 → tripwire → round-robin) | `python evals/composer-battery/drive_battery.py --round 2026-08-20-baseline --repeats 5` |
| Fire a subset / resume after an interruption | `python evals/composer-battery/drive_battery.py --round <r> --cases fork_coalesce,error_routing --resume` |
| §7 planner probe (calibration only) | `python evals/composer-battery/drive_battery.py --round <r>-calib --probe` |
| Score + report | `python evals/composer-battery/report.py --round <r>` |
| Compare with a previous round | `python evals/composer-battery/report.py --round <r> --compare <prev>` (refuses on binding-identity mismatch; `--force-compare` overrides and stamps the report FORCED/not-attributable; prints recorded deltas, skill hash first) |
| Delete this round's sessions (only complete captures) | `python evals/composer-battery/drive_battery.py --round <r> --cleanup-only` |

`--cleanup-only` skips firing entirely and only runs the cleanup for
`--round`. `--cleanup` fires normally and then also runs the cleanup
afterward. Both delete only sessions whose capture is complete
(`messages.json`, `meta.json`, `reviews.json` all present and parseable);
incomplete captures are left alone. The listing is paginated (`limit=200`,
the route's maximum; it defaults to 50), and the cleanup covers **both**
corpus sessions (`battery/<round>/<case>/<n>`) **and** the tripwire/probe
sessions (`battery/<round>/_tripwire/<fixture>/1`,
`battery/<round>/_probe/<fixture>/<arm>`) — they are this round's sessions
too, and their captures are on disk under the same names.
`--no-tripwire` skips both the tripwire firing and its classifier
pre-flight (calibration/debugging only — never for a rate-bearing round).

Other flags: `--base <url>` (default `$ELSPETH_EVAL_BASE_URL`, else
`https://elspeth.foundryside.dev`), `--env-file` (default
`deploy/elspeth-web.env`), `--state-dir` (default `~/.elspeth-battery`),
`--runs-dir` (default `evals/composer-battery/runs`). `--cases canary`
still fires the tripwire — pass `--no-tripwire` as well to fire the canary
alone. An unknown `--cases` name exits 64 naming it, before any network
call.

### Fire against the origin, not the public hostname (elspeth-ad5628ecda)

`https://elspeth.foundryside.dev` is served through Cloudflare, whose origin
read timeout cuts any response the origin has not begun answering within a
**measured ~125.0 s** and returns a 524. The origin's own composer budget is
`ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=600.0`, so the two limits disagree by
~4.8×, and the origin *cancels* the in-flight run when the edge hangs up —
the result is destroyed, not merely delayed. Round `2026-08-17-calib5`
aborted after 5 of 19 corpus cases on `3 consecutive instrument_error`; three
of those first five (`deaggregation`, `deep_routing`, `error_routing`)
exceeded the cut, and `batch_aggregation` cleared it by 0.6 s. **The corpus
cannot be measured through the hostname.**

Address the uvicorn socket directly instead — this bypasses Cloudflare, Caddy
and TLS in one step:

```bash
python evals/composer-battery/drive_battery.py \
  --base unix:///run/elspeth/uvicorn.sock --round <r> --repeats 5
```

Notes:

- This is a property of the free-tier edge in front of the **development**
  substrate, not a product defect. The ECS deployment derives its transport
  ceiling from `var.alb_idle_timeout_seconds` (`locals.tf`, with a plan-time
  cap and a test pinning the mirror), and an enterprise edge configures the
  origin timeout directly.
- The base URL is already part of the **binding identity**
  (`identity.binding.substrate`, alongside `firing.json`'s `base`), so
  `report.py --compare` refuses across transports rather than silently
  diffing an edge-fired round against a socket-fired one. `--force-compare`
  still overrides and stamps the report FORCED/not-attributable — which is
  the right call only if you have separately established that the edge did
  not perturb the round it fired.
- What the socket path removes is transport, not application behaviour: the
  same request returns a byte-identical body on both paths (verified on
  `POST /api/auth/login` and `/api/system/status`). Latency measurements will
  shed the edge's TLS and proxy overhead, which is small next to a compose.
  Verified end-to-end 2026-08-17 — `deaggregation`, which died at 125.024 s
  with a 524 through the hostname, returned **200 after 130,405 ms** over the
  socket with every instrument flag clear
  (`runs/2026-08-17-socket-verify/deaggregation/1/meta.json`).
- Nothing proxies the socket, so no intermediary remains that could impose a
  ceiling: the only deadlines left are the driver's `CLIENT_TIMEOUT_S` (620 s)
  and the origin's own `composer_timeout_seconds` (600 s), which is the
  arrangement `_validate_composer_timeout_transport_headroom` exists to keep
  ordered.
- The health check in **Prerequisites** above can be run the same way:
  `curl -s --unix-socket /run/elspeth/uvicorn.sock
  http://localhost/api/system/status | jq .composer_available`.

Exit codes, as implemented in `drive_battery.py main()` / `report.py main()`:

- driver `0` completed; `1` aborted by the instrument rules — **three
  consecutive INSTRUMENT-class exclusions** (`capture`, `truncated`,
  `read_integrity`, `auth`, `http`, `transport`, `terminal_missing`;
  canary runs count toward this streak). MEASUREMENT-class exclusions
  (`surface` — the prompt routed to the planner, or `no_calls` — the
  composer never called a tool) never count toward the abort streak and
  never abort on their own; `64` config/identity — a malformed or
  wrong-mode credentials file, a missing env budget in
  `deploy/elspeth-web.env`, an unknown `--cases` name, a tripwire/probe
  pre-flight that no longer routes P→planner / L→loop (classifier drift),
  or an unusable `/api/system/status` (identity resolution never does I/O
  beyond reading these); `70` auth — login rejected.
- `report.py`: `65` (`EX_DATAERR`) on a refused `--compare` (binding-identity
  mismatch without `--force-compare`) or a late-binding refusal (a round
  captured under a different `corpus_version` or prompt hash than the
  scenarios on disk).

## Reading a report

`runs/<round>/report.md` — headline (clean / optimal / hard, each with `n`,
excluded count and the Σ/Σ pooling formula, correct under unequal per-case
`n` after exclusions), the canary block (`n`, `non_optimal`, `flag`), the
tripwire table (its own table; never pooled), per-repeat bins, per-case
rates (indicative at N=5 — 95% CI ±X pp, one-sample half-width; see
"Calibration before freeze" below), an **Exclusions** section split into
instrument exclusions (harness faults — `capture`/`truncated`/
`read_integrity`/`auth`/`http`/`transport`/`terminal_missing`) and
**Measurement exclusions** (the composer routed a prompt to the planner or
never called a tool — product findings the loop-only instrument cannot
score, listed separately and never pooled into the rate), then the
**deviation ledger** grouped case → class with evidence (`sequence_no`
range, tool, args digest, codes, audit ordinal), then the **criteria
ledger** (case/repeat → `red_reasons`/`green_reasons`): a run can fail a
criterion — invalid or empty final state, a build sentinel, no schema read
before the first mutation — with no deviation event at all, and without
that section its `clean` would drop against an empty histogram. Like the
deviation ledger and the per-case table, it covers **corpus cases only**;
the canary is summarised in its own block (`n`, `non_optimal`, `flag`), and
a non-optimal canary is read from its `score.json`, which carries the same
`red_reasons`/`green_reasons`.
`unattributed_excess` and
`below_floor` are printed on their own headline line — a high
`unattributed_excess` rate is a taxonomy gap to fix, never a floor to
widen. A degraded firing (`report["degraded"]["reasons"]`, e.g. exclusions
above 15%, canary >1/10 non-optimal, a driver abort, a tripwire that
raised) is called out at the top of the markdown; `report["findings"]` is a
separate list — corpus/product findings that are reported but do not by
themselves mark the firing degraded.

`runs/<round>/firing.json` is the driver's own ledger (`completed`,
`aborted`/`abort_reason`, `tripwire_error`, `case_flags`). The report reads
`aborted`/`abort_reason` and `tripwire_error`; it does **not** read
`case_flags` — it re-derives the per-case exclusion streak and the 15%
exclusion flag from the captured bytes, which is the stronger reading.
`firing.json` remains the record of what the driver saw as it fired.
Triage reads the ledger; kit defects become Filigree issues by hand.

`--compare <prev>` prints `compare.recorded_deltas` — a dict keyed by
recorded-identity field, **`composer_skill_hash` always first**, then every
other differing field sorted — plus pooled and per-case rate deltas
(per-case deltas are labelled indicative; claims are made on the pooled
aggregate only).

Every `score.json` carries `red_reasons`, `green_reasons` and
`exclusion_evidence` so a single run can be read without the report.

## Calibration before freeze (spec §6) — operator procedure

Calibration runs are corpus QA. They enter no rate. Use a round name that
says so (`…-calib`).

1. **Canary at N=10**: `--cases canary` (the canary block runs at N=10 by
   design). The canary asserts the **instrument**, not optimality — pass is
   zero exclusions, `surface_observed == compose_loop` 10/10,
   `other_text_calls == 0`, and at least one run at floor. Anything else:
   stop and read the exclusions. (Do **not** expect ≥ 9/10 optimal: measured
   2026-08-17, the same prompt scored 8/10 and 2/10 at floor on consecutive
   blocks — path variance on a single-shape task is 2–5 calls, so an
   optimality threshold here reads the kit's variance as an instrument
   fault. Spec §6's original rule is withdrawn in the errata.)
2. **Tripwire**: runs automatically at the start of every round; check
   `runs/<r>/_tripwire/tripwire.json` — all three `pass: true`.
3. **Paired planner probe**: `--probe`. Read `runs/<r>/_probe/probe.md`:
   every arm `surface_ok`; write the reading against the pre-registered rule
   into `calibration/README.md`.
4. **One N=1 pass over the 18 cases**: `--repeats 1 --cases <all but canary>`
   then `report.py`. Check, per case:
   - `surface_observed == compose_loop` (an `instrument_error: surface`
     means the prompt routes to the planner — reword, re-dry-run);
   - advisor rows are on the advisor model with null `tools_spec_hash`
     (`llm_calls` in the capture); `other_text_calls` should be 0;
   - `first_call_messages_hash` stable across two runs of one case
     (fire one case twice with `--repeats 2 --cases <case>`);
   - the floor is reachable: at least one run at floor across calibration,
     else the derivation is wrong — re-derive (structural reason only) and
     record pre/post in `calibration/README.md`;
   - the data path actually taken (`inline_blob` in the `set_pipeline` args
     vs a `create_blob` detour) — record per case; a corpus-wide detour is a
     kit finding, not a floor change;
   - passivity/decline rate as a corpus-QA signal — a prompt that reads as a
     question gets tightened.

   A per-case rate at this stage is a single N=1 read: report it as the
   observed floor/deviation, not as a rate claim (rate claims need the
   pooled aggregate over a real round, see "Reading a report" above).
5. **Freeze**: bump `corpus_version: 0 → 1` in `corpus.md` and in every
   `scenarios/*/scenario.json` (`floor.post_calibration` filled in), commit
   as one change: `git commit -m "feat(evals): freeze composer battery corpus v1" -- evals/composer-battery`.
   From here any prompt or floor edit is a version bump and a new baseline.

## Known v1 limits

- **Multi-source deferred** (spec §1): `multi_source_queue` and
  `multi_flow` need multiple invented-data sources; `set_pipeline` v1
  cannot bind named/multiple blob-backed sources, so their mutation floor
  is not derivable by the pre-registered rules. Corpus v1 is single-source
  only — a named blind spot.
- **`template_lookups` inlining**: the compose surface cannot author
  repo-relative asset files, so `template_lookups`' template and lookup
  files are inlined as a `prompt_template`; its `expected_topology` is
  identical to `openrouter_sentiment`'s — the two cases currently measure
  the same shape.
- **`condition_literal` is a membership assertion**, not an attribution:
  on a multi-gate case it confirms the pinned threshold value is *present
  somewhere* in the graph's conditions, not *which* gate carries it. Only
  single-gate cases (`threshold_gate`, `schema_contracts_demo`) assert this
  today.
- **Volunteered no-op passthrough**: `explicit_routing`,
  `schema_contracts_demo`, and `canary` require the composer to volunteer a
  no-op passthrough node to match the registered floor. Watch this at
  calibration — if the composer reliably omits it, the floor derivation
  needs review, not the composer.
- **Likely second-discovery-call cases**: `deep_routing` (16 nodes — still
  cheap to score; measured 2026-08-17, 5-repetition `perf_counter` median:
  match ≈0.00005 s, worst-case rejection ≈1.21 s — method and full figures
  in the spec's errata (b); re-measure at calibration) and
  `multi_query_assessment` are the cases most likely to need a second
  discovery call before composing; watch these first at calibration.

## Operational posture

- Serial; a full 19×5 round runs a few hours. Off-peak; the OpenRouter key
  and `sessions.db` are shared with real use — say so in the round name.
- The per-user composer rate limit is 10/min; the driver's serial cadence
  stays under it.
- Sessions are titled `battery/<round>/<case>/<n>` **before** the prompt is
  posted (suppresses the unaudited auto-title call). `--cleanup` deletes
  only this round's sessions whose capture is complete; default off.
- `runs/` is git-ignored; `report.md` for a round worth keeping is copied
  into `docs/` by hand.

## Layout

| Path | What |
| --- | --- |
| `corpus.md` | verbatim prompts, `corpus_version` |
| `scenarios/<case>/scenario.json` | oracle payload, expected topology, floor + derivation, criteria |
| `drive_battery.py` | live driver (capture only) |
| `planner_probe.py` | §7 probe + tripwire |
| `report.py` | offline scoring + report |
| `calibration/README.md` | calibration decisions (pre/post floors) |
| `../lib/battery_*.py` | tracked libraries (topology, scenario, corpus, capture, score, report, planner) |
