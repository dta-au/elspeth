---
title: Collector calibration can bless the wrong policy or an invalid topology
labels: [area/tests, area/composer, type/bug]
---

The collector calibration harness decides a run is a clean sample by checking that three fields are non-empty. It does not check that the policy is the one the prompt requires, or that the resulting pipeline would validate. Wrong-policy and invalid runs therefore enter the baseline.

## Where this lives

Everything is in one standalone script: `evals/composer-battery/calibration/run_collector_calibration.py`, roughly 210 lines with its own `main()`. It is an evaluation harness, not part of the shipped product — it drives the Composer (the web authoring surface where a model builds a pipeline in conversation with a user) through a set of prompts, grades what came back, and records baselines for how many provider calls and repair turns a scenario should take.

A **collector** is a node kind that gathers rows belonging to a group before releasing them. It is configured by three fields: `scope_name` (which group), `scope_opener` (the node that starts the group) and `scope_policy` (when the group is considered complete). `scope_policy` takes `require_all` — wait for every expected row — or `best_effort`.

## What happens

A run is counted as having authored a scoped collector, and contributes to the provider-call and repair baselines, even when the collector it authored would not satisfy the scenario or would not build.

## Why

Reviewed at commit `3dc67fb1d`.

`grade()` at `evals/composer-battery/calibration/run_collector_calibration.py:69` classifies a collector as completely bound when all three fields are merely truthy:

```python
complete = [n for n in collectors if n.get("scope_name") and n.get("scope_opener") and n.get("scope_policy")]
```

The prompt being calibrated requires every section to return, which semantically requires `require_all`. But `best_effort` is a non-empty string, so it passes the same test — as would any other wrong-but-truthy value.

Then at `evals/composer-battery/calibration/run_collector_calibration.py:171`, `shape_pass` compares only whether a scoped collector was authored against whether one was expected:

```python
rec["shape_pass"] = rec["authored_scoped_collector"] == expects_collector
```

The harness loads `validate.json` in the same function but never consults its `is_valid` field when deciding whether a run counts. A composition that would fail validation is graded identically to one that would pass.

## Impact

Confined to the evaluation harness. No runtime, user-facing or shipped code path is affected, and nothing an operator runs changes behaviour because of this.

The cost is to the harness's own credibility: a semantically wrong or invalid collector graph can poison the provider-call and repair baselines, so later valid runs get graded against evidence that never satisfied the scenario. The distortion is silent — a poisoned baseline is indistinguishable from a clean one in the output.

## Fix

**Size: small and self-contained.** One file, no dependencies on the rest of the tree, no runtime risk. A reasonable first issue for someone new to the project, since it can be understood and verified without knowing the engine.

Correct behaviour:

1. `grade()` checks the *value* of `scope_policy` against what the scenario requires — `require_all` for this prompt — rather than accepting any truthy value. The required policy should come from the scenario definition, not be hard-coded in `grade()`, so other prompts can require something different.
2. A run contributes to the baseline only if its composition validates, i.e. `validate.json` reports `is_valid` true.
3. Runs excluded for either reason keep a diagnostic record naming the reason, instead of being silently dropped or silently counted.

You would know it holds by two cases: a run that authors `best_effort` against this prompt, and a run whose `validate.json` reports `is_valid: false`. Both must be excluded from the baseline population, and both must appear in the harness output with their exclusion reason. Feeding the harness a recorded run of each kind is enough to check this — no live provider calls are needed.
