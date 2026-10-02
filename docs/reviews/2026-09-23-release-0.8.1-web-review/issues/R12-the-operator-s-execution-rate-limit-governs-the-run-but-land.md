# R12. The operator's `execution_rate_limit` governs the run, but Landscape records the engine-default `rate_limit`

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-18#1 |

## Finding

- **Status and severity:** confirmed, **medium**.
- **Location:** `src/elspeth/web/execution/service.py:3629-3636`. For comparison, the telemetry and export policies are folded into `settings` at `service.py:3442-3455`, and `_approval_inputs_from_frozen` is at `:1141-1145` and `:1234-1236`.
- **What is wrong:** 24dccdd9e builds the `RateLimitRegistry` from `WebSettings.execution_rate_limit` but leaves `settings.rate_limit` at the engine default. `audit_safe_resolved_config` then persists `settings.rate_limit` to Landscape and into the approval config hash. The comment at `service.py:3629-3636` itself says `settings.rate_limit` "is only ever the engine default".
- **Failure scenario:** The operator sets `services.azure_openai.requests_per_minute=30`. Runs are throttled at 30/min, but Landscape records `default_requests_per_minute=60` with empty services. The config hash is identical across runs made under different operator limits, so a reproduction or a timing and capacity-retry diagnosis from the audit record uses the wrong throttle. A resumed run takes the resuming instance's limit, and nothing records that.
- **Suggested fix:** Apply `settings = settings.model_copy(update={'rate_limit': self._settings.execution_rate_limit})` both at `:3451-3455` and in `_approval_inputs_from_frozen`, then build the registry from `settings.rate_limit`. As with telemetry and export, an operator change between approval and execution will then refuse the governed run at the `:3552` hash recheck. That consequence is intended.
- **Sources:** be-18#1.
- **Verifier notes:** Nothing else records the effective limit. The YAML importer rejects `rate_limit` (`yaml_importer.py:116`), and no `core/landscape` module refers to it.


## Source findings and verification

### be-18#1: Operator execution_rate_limit governs the run but Landscape records the engine-default rate_limit

- **Reported at:** `src/elspeth/web/execution/service.py:3631`; reviewer severity medium; category audit-integrity; diff-anchored True.
- **Summary:** 24dccdd9e builds the RateLimitRegistry from self._settings.execution_rate_limit but leaves settings.rate_limit at the engine default. pipeline_config.config = audit_safe_resolved_config(settings) -> resolve_config(settings) persists rate_limit verbatim to Landscape and into the approval config hash. The operator telemetry and export policies are folded into settings precisely so Landscape records the effective policy.
- **Failure scenario:** The operator sets ELSPETH_WEB__EXECUTION_RATE_LIMIT={"services":{"azure_openai":{"requests_per_minute":30}}}. The run is throttled at 30/min, but its Landscape config records rate_limit default_requests_per_minute=60 with empty services, and the config hash is identical across runs made under different operator limits. Reproducing the run, or explaining its timing and capacity-retry failures, from the audit record gives the wrong throttle. Resumed runs take the resuming instance's limit, and nothing records it.
- **Evidence:** service.py:3629-3636 (the comment itself says settings.rate_limit 'is only ever the engine default'); service.py:3442-3455 folds the telemetry and export policies into settings before PipelineConfig; the _approval_inputs_from_frozen docstring at :1141-1145 says operator settings 'also enter the Landscape run config'; preflight.py:597 calls resolve_config; core/config.py:3103-3137 fingerprints only secret-bearing free-form sections, not rate_limit.
- **Suggested fix:** Apply settings = settings.model_copy(update={'rate_limit': self._settings.execution_rate_limit}) both at :3451-3455 and in _approval_inputs_from_frozen at :1234-1236, then build the registry from settings.rate_limit. Intended consequence, the same as for telemetry and export: an operator change between approval and execution refuses the governed run via the :3552 hash recheck.
- **Verifier (trace):** upheld, confidence high, severity medium. The finding holds at the pinned commit 74c0ce0db. 24dccdd9e is in the review window (it is at position 106 of `git log 7c986dc97..74c0ce0db`). It builds the run's RateLimitRegistry from WebSettings.execution_rate_limit (service.py:3631, 3636) but never folds that value into `settings`. `settings.rate_limit` therefore stays at the engine default, and that default is what reaches Landscape and the approval hash. Nothing upstream sets it: the composer YAML importer rejects `rate_limit` (yaml_importer.py:116), and operator_marked_config_dict only touches landscape.export (export_marking.py:12-26). Nothing downstream records the effective limit either: the orchestrator only passes the registry into the PluginContext (core.py:128/145, run_context_factory.py:92/155), and no core/landscape module refers to rate_limit. Before this commit the recorded default and the enforced default were the same. Since it, an operator override makes the recorded config wrong, and the config hash no longer changes when the operator changes the limit. No ruling in docs/agents/recent-code-hints.md or elsewhere in docs mentions execution_rate_limit. Severity stays medium: row data is not changed, but the audit record states the wrong throttle, which affects timing and the capacity-retry outcomes the finding describes, and the operator-policy folding pattern shown below was applied inconsistently.
