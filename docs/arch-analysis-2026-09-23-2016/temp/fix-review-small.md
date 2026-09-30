# Fix review: K103, K123, K051 (direct review, release/0.8.1 @ ffd704d1a)

Each fix was checked against the verified claim in `verified-concerns.md`. For each, the focused tests were run at head, and then the fix's `src` or deploy hunk was reverted in a throwaway detached worktree (`elspeth.__file__` confirmed inside it) to show that the tests go red.

## K103: `elspeth web --auth` default overwrote the env auth provider (High) → **CLOSED**

- **Fix** (`45fa83e48`, `src/elspeth/cli.py:4744-4792`): `--auth` now defaults to `None`, and the CLI uses the explicit flag when given, else `os.environ["ELSPETH_WEB__AUTH_PROVIDER"]`, else `"local"`.
- **Reach check:** `settings_from_env()` reads only `os.environ` (`web/config.py:1548`). Every shipped bundle populates `os.environ`: systemd via `EnvironmentFile=` (`deploy/linux-systemd/elspeth-web.service:11`), the ECS task env (`modules/scenario/locals.tf:512`), and ACA (`workload.bicep:371`). So an env-file provider is not missed.
- **Tests:** `tests/unit/cli/test_web_command.py` passes at head (30 passed). With the fix reverted, `test_env_configured_sso_reaches_app_factory[entra]` and `[oidc]` fail (2 failed, 28 passed). `test_explicit_auth_overrides_environment` covers the override direction.
- **Residual (Low):** the fallback literal `"local"` duplicates the `WebSettings.auth_provider` default (`web/config.py:194`). If one default changes without the other they diverge. Not a defect today.

## K123: Scenario C gateway sidecar missing required env (High) → **CLOSED**

- **Fix** (`97616582f`): six variables were added through the module, the root and the tfvars example (`OAUTH_AUTH_METHOD`, `MAX_MESSAGES`, `MAX_TOOLS`, `MAX_STRING_CHARS`, `MAX_SCHEMA_BYTES`, `MAX_SCHEMA_DEPTH`). Each has a validation: a positive integer under `custom_gateway`, and must be unset under bedrock.
- **Measured:** the gateway container env in `modules/scenario/ecs.tf` now names exactly the 13 names in `load_config`'s required tuple (`gateway/src/elspeth_llm_gateway/core/config.py:294-308`), and 0 names are outside `KNOWN_ENV`. That matters because the loader also fails on `unknown_env`. The example values load through the real `load_config`; the only error was the dummy model-mappings placeholder in my probe.
- **Test:** `test_gateway_sidecar_supplies_every_required_runtime_environment_name` asks the gateway's own loader for its required set, with a positive and a negative control, so it tracks the runtime contract.
- **Residual (Low):** the test asserts required ⊆ supplied but not supplied ⊆ `KNOWN_ENV`. A future stray or renamed variable would pass the test and still stop the sidecar from booting (`unknown_env`). Adding the second assertion is one line. A live `terraform plan` or ECS start was not run.

## K051: gate opened NodeStateGuard at attempt=0 on reclaimed or resumed work (High) → **CLOSED**

- **Fix** (`999538760`, plus tests `884e481b8` and `d5c3c8e09`): `GateExecutor.execute_config_gate` takes `attempt_offset` and now records `attempt = token.resume_attempt_offset + attempt_offset` and `resume_checkpoint_id` (`engine/executors/gate.py:273-332`). This is the same shape as the transform executor (`transform.py:781`). `token_traversal` threads the offset it already carried for transforms (`token_traversal.py:522-548, 1075-1210`).
- **Callers:** there is one production caller (`token_traversal.py:542`); `gate.py:142` is a docstring example.
- **Tests:** the new `tests/unit/engine/test_gate_claim_recovery.py`, `test_executors.py` and the fork/join property test give 293 passed at head. With the fix reverted, **4 fail with the exact `LandscapeRecordError … IntegrityError`** the finding predicted (lease-claim, checkpoint-resume, reclaimed second attempt, and resume fork→coalesce through a gate branch).
- **Residual (Low):** `attempt_offset: int = 0` is a defaulted keyword on three layers. That was the existing pattern for transforms, but a future caller that forgets it silently gets the old collision. Making it required keyword-only at the executor boundary would close that off. Not a defect today (single caller, passes it).
