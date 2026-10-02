# R51. A bad `execution_rate_limit.persistence_path` is accepted at boot and fails every web run

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-02#1 |

## Finding

- **Location:** `src/elspeth/web/config.py:511` and `contracts/config/runtime.py:55-75`.
- **Wrong:** The path rules run only per run in `RuntimeRateLimitConfig.from_settings` (`service.py:3631`).
- **Fix:** Add a `WebSettings` after-validator that resolves the path against `data_dir`.
- **Sources:** be-02#1.


## Source findings and verification

### be-02#1: execution_rate_limit.persistence_path accepted at boot, fails every web run

- **Reported at:** `src/elspeth/web/config.py:511`; reviewer severity low; category config-validation; diff-anchored True.
- **Summary:** RateLimitSettings.persistence_path has no validator. Its path rules (no URI, must resolve under data_dir) run only in RuntimeRateLimitConfig.from_settings, called per run at execution/service.py:3631. Nothing checks them at WebSettings construction, lifespan or readiness.
- **Failure scenario:** Operator sets ELSPETH_WEB__EXECUTION_RATE_LIMIT={"persistence_path":"/var/lib/elspeth/rl.db"} with data_dir=/data, or uses '../rl.db'. Boot and readiness pass. Every web-executed run raises ValueError('rate_limit.persistence_path escapes allowed root ...') before the orchestrator starts. The except BaseException at service.py:4048 catches it and marks the run failed, so the misconfiguration shows up first as user run failures.
- **Evidence:** grep -rn execution_rate_limit src: only config.py:511/1446 and execution/service.py:3631. contracts/config/runtime.py:55-75 holds the only validation (_resolve_rate_limit_persistence_path). core/config.py:1885 declares persistence_path as a plain str | None.
- **Suggested fix:** Add a WebSettings model_validator(mode='after') that calls RuntimeRateLimitConfig.from_settings(self.execution_rate_limit, state_dir=self.data_dir), or _resolve_rate_limit_persistence_path, so a bad path fails at boot. Pin it with an escaping-path test.
- **Verifier (trace):** upheld, confidence high, severity low. I traced the failure path and it is real. At the pinned commit, nothing checks WebSettings.execution_rate_limit.persistence_path at boot. The only place the path rule is enforced is RuntimeRateLimitConfig.from_settings, which runs once per web run inside _run_pipeline's try block. A path outside data_dir, or a URI, therefore passes WebSettings construction and startup. Every web run then raises ValueError and is marked failed. I found no validator and no recorded ruling that covers this. Low severity is right: the fault is operator misconfiguration, it fails closed, and the error message names the setting. But the problem is not reported at boot, and users see it first as failed runs.
