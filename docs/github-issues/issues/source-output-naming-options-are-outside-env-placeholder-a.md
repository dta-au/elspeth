---
title: Source output-naming options are outside env-placeholder admission authority
labels: [area/plugins, type/bug]
---

Transforms declare which of their options reach output names, and the env-placeholder
admission check consults that declaration. Source plugins expose output-name options with
no equivalent authority, so those keys fall outside admission and fingerprinting.

## Why

The transform side has a closed declaration,
`BaseTransform.output_naming_config_keys` (`src/elspeth/plugins/infrastructure/base.py:560`),
and the loader consults it when building the set of output-reaching options — but only for
transforms. `src/elspeth/config_loading.py:109-110` gates that consult on
`if kind == "transform":` before iterating `plugin_class.output_naming_config_keys`.

There is no source-side counterpart to iterate, so a source option that names an output
is not admitted through the same authority.

## Impact

Env placeholders in source output-naming options are outside the admission check that
covers the equivalent transform options, and those keys are not covered by fingerprinting.
This is the residue of an earlier transform-side fix, which closed the transform half of
the same gap.

## Fix

Inventory the source plugins that expose output-name options, add a closed source-side
declaration equivalent to `output_naming_config_keys`, and extend env-placeholder
admission and fingerprinting to cover the declared keys. The declaration must be explicit
per plugin; inferring these options from heuristic name matching is not acceptable, for
the same reason the transform side declares them.

Stated limit: this needs a source contract design and a registry migration, so it is
larger than an incidental fix alongside the transform-side change.
