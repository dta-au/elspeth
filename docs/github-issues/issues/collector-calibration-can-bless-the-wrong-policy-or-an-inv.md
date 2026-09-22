---
title: Collector calibration can bless the wrong policy or an invalid topology
labels: [area/tests, area/composer, type/bug]
---

The collector calibration harness grades a run as a clean sample on the presence of three fields, without checking that the policy is the one the prompt requires or that the resulting composition validates. Wrong-policy and runtime-invalid runs can therefore enter the baseline population.

## What happens

A calibration run is counted as having authored a scoped collector, and contributes to the provider-call and repair baselines, even when the collector it authored would not satisfy the scenario or would not build.

## Why

Reviewed at `3dc67fb1d`.

`grade()` in `evals/composer-battery/calibration/run_collector_calibration.py:69` classifies a collector as completely bound when `scope_name`, `scope_opener` and any truthy `scope_policy` are present:

```python
complete = [n for n in collectors if n.get("scope_name") and n.get("scope_opener") and n.get("scope_policy")]
```

The calibrated prompt requires every section to return, which semantically requires `require_all`. `best_effort` — or any other truthy value in that field — passes the same test.

At `evals/composer-battery/calibration/run_collector_calibration.py:171`, `shape_pass` compares only `authored_scoped_collector` against `expects_collector`. The harness reads `validate.json` in the same function but does not consult `is_valid` when deciding whether a run is a clean sample.

## Impact

Limited to the calibration harness and anything derived from it — no runtime or user-facing path is affected. The cost is that a semantically wrong or runtime-invalid collector graph can poison the provider-call and repair baselines, so later valid runs are graded against evidence that never satisfied the scenario in the first place. The distortion is silent: a poisoned baseline looks exactly like a clean one.

## Fix

- Grade the required collector policy and scope topology explicitly, including `require_all` for this prompt, rather than accepting any truthy `scope_policy`.
- Require a valid composition before a run contributes to the baseline.
- Keep a diagnostic record for wrong-policy and invalid-state failures instead of discarding them, so the harness can show why a run was excluded rather than quietly treating it as a clean sample.

Done looks like: a run authoring `best_effort` against this prompt, and a run whose `validate.json` reports `is_valid: false`, are both excluded from the baseline population and both appear in the harness output with the reason for exclusion.
