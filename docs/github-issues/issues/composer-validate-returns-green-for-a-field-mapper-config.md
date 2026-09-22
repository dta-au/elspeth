---
title: Composer validate() reports no errors for a field_mapper config that can never construct
labels: [area/composer, type/bug]
---

`CompositionState.validate()` returns `is_valid=True` with an empty error list for a transform whose options can never build, so the composer gives a clean bill of health to a pipeline the runtime will reject.

## What happens

`field_mapper` declares `select_only: bool` (`src/elspeth/plugins/transforms/field_mapper.py:172`). Giving it a non-boolean value is a plain, permanent configuration error — not an unfinished draft. Composer validation reports nothing; the runtime path raises `PluginConfigError` from `instantiate_plugins_from_config` for the same configuration. This was reported as reproduced on a clean tree, independent of any in-flight work, at the time of filing. The reproduction recorded with the report drove a unit-test helper for the rule C schema-contract check that no longer exists under that name, so the snippet needs reconstructing before it can be re-measured; the divergence it describes is validate-green, runtime-red.

## Why

The composer's probe helpers in `src/elspeth/web/composer/state.py` — `_probe_transform_output_schema` (line 4754), `_probe_transform_declared_inputs` (line 4835) and their sink and source siblings — construct the plugin in order to read its contract facts. Construction failures are filtered through `_is_config_probe_exception` (line 2004) and the probe abstains with an empty result.

That abstention is deliberate and documented in the helpers themselves: a draft node whose options do not yet build is owned by the existing config-validation paths, and these rules must not turn an incomplete draft into a hard error. The gap is that a draft which can *never* construct is not the same as one that is merely incomplete, and the probe boundary cannot currently tell them apart, so it stays silent for both. Because every probe abstains, the node contributes no contract facts at all, and downstream rules that would otherwise fire also go quiet.

## Impact

The composer is one of two authoring surfaces, and its premise is that validation is part of the workflow rather than an after-the-fact diagnostic. A green result that cannot distinguish "your configuration is fine" from "your configuration is unbuildable and I could not tell" weakens that premise, and the author has no way to see the difference until a run fails.

## Fix

The seam needs a way to say "this construction failure is permanent, not pending". Two directions, neither prescriptive:

- Have the config-validation path, which already runs and already holds the `PluginConfigError`, surface it as a blocking validation entry instead of leaving the probes to notice and abstain.
- Widen the taxonomy at the probe boundary: a type error on a declared field is permanent, whereas a missing required field on a half-written draft is pending. A closed exception set already exists — `test_rule_c_unexpected_constructor_exception_propagates` (`tests/unit/web/composer/test_state.py:5722`) pins that anything outside `_is_config_probe_exception` must propagate rather than be silently deferred to execution — so this may be a matter of widening it at the right site rather than inventing one.

Do not fix this by making the probes raise on any construction failure: that turns every incomplete draft into a hard error, which is the behaviour the abstention was added to prevent.

`tests/integration/pipeline/test_composer_runtime_agreement.py` is the home for this divergence class; it already hosts comparable cases, such as `TestComposerRuntimeFixedModeImplicitRequiredAgreement` (line 4217).
