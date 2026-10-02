---
title: Shipped examples that use environment variables are never validated as real settings
labels: [area/tests, type/bug]
---

The end-to-end test that certifies the shipped examples checks raw YAML shape for any example referencing an environment variable, and never loads those files the way production does. A configuration that real startup would reject can therefore ship looking certified.

## Where to start

`tests/e2e/examples/test_shipped_examples.py`, plus the example configurations under `examples/`. The production entry point the test is bypassing is `load_settings()`, which expands `${VAR}` references and builds a validated `ElspethSettings` object — the thing the engine actually runs from.

## What happens

The test splits examples in two on `_needs_env_vars`:

- Examples with no `${VAR}` references go through `test_local_examples_load_via_config_system`, which calls `load_settings()` and asserts a real `ElspethSettings` instance with populated sources and sinks.
- The rest go through `test_env_var_examples_are_structurally_valid`, which reads the file with `yaml.safe_load` and asserts only that the required top-level keys and per-plugin keys are present.

The second test's docstring states the reason plainly: `load_settings()` cannot be called, because expansion would fail where those variables are not set.

## Why

The exemption is a static allowlist, `_EXAMPLES_WITH_ENV_VARS` (`test_shipped_examples.py:41`), naming 11 example directories. Those directories hold 20 `.yaml` files, three of which are declared auxiliary — lookup tables and fault-injection configs listed in `_AUXILIARY_EXAMPLE_YAMLS`, not pipelines. That leaves 17 pipeline configurations exempt from full validation. The audit that raised this recorded 16, so the exempt set has grown since it was written.

## Impact

These are the examples a newcomer is most likely to copy, because they are the ones that talk to cloud storage, key vaults, search indexes and hosted models. Anything `load_settings()` would reject — an unknown key, a wrong type, a node naming a sink that does not exist — passes the shape check in silence.

The audit reported one such class: missing `on_success`/`on_error` wiring in `examples/azure_blob_sentiment/settings.yaml`, `examples/azure_blob_sentiment/settings_pooled.yaml` and `examples/azure_openai_sentiment/settings_pooled.yaml`. That has not been re-verified node by node here, and it illustrates the gap rather than defining it.

## Fix

Every maintained example should be validated through the path production uses, with no exemption for environment variables. Done looks like `load_settings()` succeeding for every discovered example configuration and the allowlist deleted — or reduced to entries that each carry a recorded reason that is not "it has environment variables".

One decision comes first, and it is the only part that is not mechanical: how to supply values for expansion. The audit produced its finding using safe placeholders, so the approach is known to work, but where the placeholders live is open. Three plausible options, smallest first: set placeholder values in the test environment for the duration of the run; pass `load_settings()` an explicit caller-supplied mapping to expand from; or add a validation mode that accepts an unresolved `${VAR}` as an opaque string. The second is the most honest about what is being tested, since it never mutates global state; the third changes production code to serve a test and needs the strongest justification. Choose one and record why in the test.

## Size

The test change is small. The work behind it may not be. Turning the gate on will surface whatever is currently latent across those 17 configurations, and nobody has measured what that is. Run validation over them locally before committing to an estimate, and expect to fix example configurations as well as the test.
