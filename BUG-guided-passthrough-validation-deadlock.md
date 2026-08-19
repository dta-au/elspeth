# Bug: guided pass-through synthesis is an unrecoverable dead end when the sink contract fails validation

- **Reported**: 2026-08-18
- **Revision**: `07c803703a27ba78e0d0c9486200f4e03a9f6d12` (`release/0.7.2`)
- **Environment**: DTA-Dev AWS ECS, image `0.7.2-RC-180826`, task definition `elspeth-web:21`
- **Observed session**: `847ef691-54aa-4dd0-b09e-90e0579934d9`, `user_id=johnm`
- **Severity**: High — the guided flow becomes permanently unusable for the affected
  session with no operator-actionable message and no path forward.

## Summary

When the server-synthesized zero-transform pass-through sketch fails candidate
validation, the guided flow terminates with `VALIDATION_FAILED` and **cannot
recover on retry**. The sketch is derived deterministically from the reviewed
source and reviewed output, so every subsequent attempt re-derives a
byte-identical pipeline, hits the identical rejection, and fails identically.
There is no repair loop on this path, and no provider call, so nothing can vary
between attempts.

The user-visible symptom reads as "the model keeps trying the same thing and
never converges". It is not the model. The model is never invoked.

## Observed behaviour

Two operations, seven seconds apart, both terminal:

```
01:05:23 composer.guided_planner_failure
  planner_code=VALIDATION_FAILED rejection_codes=['plugin_options_invalid']
  operation_id=82b963e2-cb7a-4212-b55a-4fec78e1741c surface=guided_staged
  session_id=847ef691-54aa-4dd0-b09e-90e0579934d9

01:05:30 composer.guided_planner_failure
  planner_code=VALIDATION_FAILED rejection_codes=['plugin_options_invalid']
  operation_id=42677625-e95c-4a73-ac39-845176c6f3c8 surface=guided_staged
  session_id=847ef691-54aa-4dd0-b09e-90e0579934d9
```

Both carried `exc_class=PipelinePlannerError` with frames terminating at
`pipeline_planner.py:2775`. The session then fell back to
`authoring_surface=compose_loop`.

## Call chain

| # | Function | Location |
|---|---|---|
| 1 | `post_guided_respond` | `web/sessions/routes/composer/guided.py:2592` (planner call at `:4876`, failure handling at `:5182`) |
| 2 | `plan_guided_pipeline` | `web/composer/service.py:3532` (sketch built at `:3710-3741`, call at `:3754`) |
| 3 | `guided_reviewed_sink_options` | `web/composer/guided/planning.py:1208` |
| 4 | `_sink_options_with_declared_required_fields` | `web/composer/guided/planning.py:1162` (merge at `:1202-1204`) |
| 5 | `prepare_pipeline_plan` | `web/composer/pipeline_planner.py:2902` (call at `:2985`, conversion at `:3014`) |
| 6 | `_build_valid_pipeline_plan` | `web/composer/pipeline_planner.py:2726` (raise at `:2775`) |
| 7 | `build_set_pipeline_candidate` | `web/composer/tools/sessions.py:466` (emit at `:1153`) |
| 8 | `_prevalidate_sink` | `web/composer/tools/_common.py:2675` |
| 9 | `_validate_contract_fields_subset` | `contracts/schema.py:267` (raise at `:290`) |

## Root cause — CONFIRMED

The defect is structural, at `pipeline_planner.py:2985-3018`.

`plan_guided_pipeline` invokes `prepare_pipeline_plan` with:

```python
model_identifier="composer-guided-passthrough-synthesis",
model_version="composer.guided-passthrough-synthesis.v1",
provider="server",
repair_count=0,
```

`prepare_pipeline_plan` calls `_build_valid_pipeline_plan` exactly once and, on
`_PipelineCandidateRejected`, converts it directly into a terminal
`PipelinePlannerError(code="VALIDATION_FAILED")`. Its own comment names the
asymmetry: it carries the rejection codes "exactly as the model-driven
exhaustion path does (`_rejection_exhausted`)" — but it does not carry that
path's repair or exhaustion behaviour. There is no retry, no repair, and no
provider in the loop.

Because the sketch inputs are fixed reviewed state, the failure is a fixed
point. Retrying is guaranteed to fail identically, forever.

## Trigger — INFERRED, NOT CONFIRMED

`plugin_options_invalid` on an output is emitted by `build_set_pipeline_candidate`
at `sessions.py:1153`, when `_prevalidate_sink` rejects the merged sink options.

The most probable source of that rejection is the `required_fields` merge:
`_sink_options_with_declared_required_fields` (`planning.py:1202`) unions step-2's
declared output fields into `options.schema.required_fields`, and for explicit
schemas (`mode=fixed/flexible`) `_validate_contract_fields_subset`
(`contracts/schema.py:267`) requires those fields to be a subset of the schema's
declared field names, raising at `:290` otherwise.

This is consistent with all available evidence but is **not proven**. See
"Diagnosability" below for why it could not be confirmed.

Note that `guided_unproducible_output_fields` (`planning.py:1230`) exists
specifically to pre-empt this class of problem — its docstring describes a
declared sink field "that appears in no source's observed columns and in no
source's explicitly declared schema fields" and warns it otherwise becomes an
opaque violation "the planner burns its repair budget on". It is advisory only,
and on this path nothing acted on its output.

## Diagnosability defect (secondary bug)

The validator's message — the only artifact naming the offending field — is not
recoverable after the fact:

- `_log_guided_planner_failure` (`service.py:231`) logs `planner_code`,
  `rejection_codes`, and `error_detail=str(exc)[:300]`. The latter is the generic
  `PipelinePlannerError` message ("server-derived pipeline failed candidate
  validation"), **not** the per-option validator detail.
- The route's terminal log (`guided.py:5192`) carries only `exc_class`, frames,
  and `request_id`.
- `fail_guided_operation_with_audit` (`guided.py:5205`) persists `failure_code`
  and `unproducible_output_fields` — not the message.

So the detail exists only in the HTTP response envelope. Once the response is
gone, the cause is unrecoverable from logs or from the session database. This
directly contradicts the intent recorded at `pipeline_planner.py:2170-2180`,
where `plugin_options_invalid` carries the validator's message as `detail`
precisely so the failure is repairable — and which cites a prior incident
(run 06c9ec49, 2026-07-29) caused by withholding exactly this information.

## Reproduction

1. Start a guided session; upload and review a source.
2. In step 2, declare one or more output fields not present in the source's
   observed columns / not in the sink schema's declared fields.
3. Advance. The server synthesizes the pass-through sketch and it fails
   candidate validation with `plugin_options_invalid`.
4. Retry. It fails identically. It will never succeed.

## Suggested fixes

1. **Make the path recoverable.** Either give the server-derived path a repair
   step, or — better, since there is no provider to repair with — detect the
   condition before synthesis and return an actionable operator message instead
   of a terminal 5xx-class failure.
2. **Act on `guided_unproducible_output_fields`.** It already computes the gap
   before any pipeline is built. `plan_guided_pipeline` should consult it and
   refuse with a specific, named-field message rather than synthesizing a sketch
   that is known-invalid.
3. **Persist the rejection detail.** Record the validator's message alongside
   `failure_code` on the guided operation, or include it in the terminal log.
   Without it, this class of failure is undiagnosable in production.
4. Consider whether `_sink_options_with_declared_required_fields` should decline
   to merge fields that would violate the subset rule, rather than producing
   options it knows the validator will reject.

## Confirming the trigger

Reproduce and capture the failing HTTP response body. `plugin_options_invalid`
carries the validator's message as `detail`, which names the offending field and
settles item 4 above.
