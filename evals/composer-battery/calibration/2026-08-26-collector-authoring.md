# Collector-authoring calibration — 2026-08-26 (freeform surface)

Round `2026-08-26-collector-calibration-freeform`, driven by
`run_collector_calibration.py` against the live service over
`unix:///run/elspeth/uvicorn.sock`. Run artifacts live under
`evals/composer-battery/runs/` (gitignored); the numbers of record are here.

## Result

**Prose derives a complete scoped collector, 3/3, with no repair and no
rejection.** Every run authored a collector whose scope binding was complete
(`scope_name` + `scope_opener` + `scope_policy: require_all`) and whose state
validated.

| run | provider calls | collector | scope binding | valid |
|---|---|---|---|---|
| corpus-register #1 | 13 | `assemble_doc` | `document_sections` ← `explode_sections`, require_all | yes |
| corpus-register #2 | 9 | `gather_document` | `sections` ← `explode_sections`, require_all | yes |
| corpus-register #3 | 11 | `gather_doc` | `doc_sections` ← `split_sections`, require_all | yes |
| url-prompt #1 | 4 | none (expected) | — | n/a |

Zero rejection codes across all four runs.

## Baseline

```
max_provider_calls: 13   # observed MAXIMUM across clean runs, not a mean
max_repair_turns:   null # not measured on this surface — see below
```

The ceiling is the observed maximum, not an average: the grading rule is "no
worse than calibrated", so a mean would fire on ordinary model variance.

`max_repair_turns` is deliberately null rather than `0`. The battery's message
capture returned no `planner_attempt_audit` rows at all in these runs, so a
zero would assert a clean result the harness never actually observed. The
grader records `attempt_phases` precisely so "no repairs" stays distinguishable
from "never looked", and the report carries `repair_signal_observed: false`.

## Why this is the freeform surface and not the guided browser walk

The ADR-031 collector scenario in
`tests/e2e/tutorial-reliability.staging.spec.ts` was the original calibration
vehicle. Five firings never reached a measurement: each one stalled on a guided
wizard turn type the driver could not answer (single-select, then schema form),
at roughly fifteen minutes and real provider spend per firing. None of those
turns are part of what the calibration measures.

The browser scenario keeps its ADR-031 job — evidence that the guided lane can
author a collector at all — and stays in recording mode with a null baseline.
Its `COLLECTOR_BASELINE` must NOT be filled from the numbers above: a guided
walk pays for wizard turns the freeform planner never makes, so these figures
would grade a cost that surface was never measured at.

## The URL prompt does not author on this surface (pinned, not a defect)

The scenario's canonical prompt points at
`https://dta-au.github.io/elspeth/tutorial-site/multi-doc-sections.json`. On
freeform the planner declines to author from it, and is right to: it has no
authoring-time URL fetch, and the source-integrity rules forbid guessing field
names it has not seen. The decline is well-reasoned rather than confused — the
reply names the intended design outright (`line_explode` opening a
per-document EXPAND scope, a `collector` closing it with
`scope_policy: require_all`). Measured 0/3 at 3 provider calls, then re-pinned
as a single expected-refusal case at 4 calls.

The corpus-register variant asks for the identical collector shape in the
register `corpus.md` requires (operator voice, task never implementation,
invented data with named fields, explicit output), which is why it authors.

## Re-running

```bash
.venv/bin/python evals/composer-battery/calibration/run_collector_calibration.py
```

Exit 0 when every expects-collector run authored a complete scoped collector
and the refusal case still refuses.
