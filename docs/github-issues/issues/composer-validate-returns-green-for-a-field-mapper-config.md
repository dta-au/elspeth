---
title: Composer validate() reports no errors for a field_mapper config that can never construct
labels: [area/composer, type/bug]
---

`CompositionState.validate()` returns `is_valid=True` with an empty error list for a transform whose options can never build, so the composer gives a clean bill of health to a pipeline the runtime will reject.

**Where this lives.** The Composer is ELSPETH's web authoring surface for pipelines. Its validation runs in `src/elspeth/web/composer/state.py`; the runtime that builds the same pipeline for real is `instantiate_plugins_from_config` in `src/elspeth/plugins/infrastructure/runtime_factory.py:86`. This issue is about the two disagreeing.

## What happens

The `field_mapper` transform declares `select_only: bool` (`src/elspeth/plugins/transforms/field_mapper.py:172`). Giving it a non-boolean value is a plain, permanent configuration error — not an unfinished draft. Composer validation reports nothing. The runtime raises `PluginConfigError` for the same configuration.

This was reported as reproduced on a clean tree at the time of filing. The reproduction recorded with the report drives a unit-test helper that is no longer in the tree, so it needs rebuilding before it can be re-measured; the behaviour it describes is validation green, runtime red.

## Why

To check a node, the composer constructs the plugin and reads what it declares about its inputs and outputs. Those construction calls are wrapped by probe helpers in `src/elspeth/web/composer/state.py` — `_probe_transform_output_schema` (line 4754), `_probe_transform_declared_inputs` (line 4835), and sink and source equivalents. When construction raises, the failure is passed to `_is_config_probe_exception` (line 2004) and the probe returns an empty result instead of an error.

That silence is deliberate and documented in the helpers: a half-written node whose options do not yet build is the business of the ordinary config-validation paths, and these checks must not turn an unfinished draft into a hard error. The gap is that a node which can *never* build is not the same as one that is merely unfinished, and the probe cannot currently tell the two apart. Because the probe returns nothing, the node supplies no information about its inputs or outputs at all, so later checks that depend on that information also stay quiet — one bad option silences more than its own rule.

## Impact

The Composer is one of two authoring surfaces, and its premise is that validation happens while you author rather than after you run. A green result that cannot distinguish "your configuration is fine" from "your configuration cannot build and I could not tell" weakens that premise, and the author sees no difference until a run fails.

## Fix

Correct behaviour: a configuration error that can never be resolved by finishing the draft — a wrong type on a declared option — is reported by `validate()` as a blocking error naming the node and the option. A genuinely unfinished draft still validates quietly, as it does today.

Two directions, and picking between them is the first task:

- Let the config-validation path, which already runs and already holds the `PluginConfigError`, report it as a blocking validation entry, instead of leaving the probes to notice and stay silent.
- Or sort the exceptions at the probe boundary: a type error on a declared option is permanent, a missing required option on a half-written node is not. A closed set of tolerated exceptions already exists — `test_rule_c_unexpected_constructor_exception_propagates` (`tests/unit/web/composer/test_state.py:5722`) pins that anything outside `_is_config_probe_exception` must propagate rather than be deferred to run time — so this may be a matter of widening that set at the right place rather than inventing one.

Do not fix this by making the probes raise on any construction failure: that turns every unfinished draft into a hard error, which is what the silence was added to prevent.

You would know it holds from a test in `tests/integration/pipeline/test_composer_runtime_agreement.py` — the file that exists to pin composer and runtime agreeing — that gives `select_only` a non-boolean value and asserts both halves: `validate()` reports a blocking error, and the runtime rejects the same configuration. A second test must show an unfinished draft still validating without error, or the fix has traded one defect for another.

Size: not a one-line change. The code edit is small either way, but the choice between the two directions wants a decision before anyone starts, and the regression test has to cover both sides.
