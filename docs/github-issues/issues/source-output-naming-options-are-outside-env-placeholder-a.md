---
title: Source output-naming options are outside env-placeholder admission authority
labels: [area/plugins, type/bug]
---

Transform plugins declare which of their options name a field that reaches the output, and
a loader check consults that declaration before allowing an environment placeholder in
one. Source plugins expose output-naming options with no equivalent declaration, so those
options fall outside the check.

The work spans two files: `src/elspeth/config_loading.py`, which performs the check, and
`src/elspeth/plugins/infrastructure/base.py`, which holds the plugin base classes
(`BaseTransform` at line 194, `BaseSource` at line 2095).

## Why

Configuration values may contain `${VAR}` environment placeholders. When such a value also
names an output field, expansion can turn the placeholder into a valid field name, after
which plugin validation accepts it and the value is written as a row key and as a
downstream artifact header — so the set of options that can name output has to be known
and declared, not guessed.

The transform side has that declaration:
`BaseTransform.output_naming_config_keys` (`src/elspeth/plugins/infrastructure/base.py:560`).
The loader consults it at `src/elspeth/config_loading.py:109-110` — but only for
transforms, gated on `if kind == "transform":` before iterating
`plugin_class.output_naming_config_keys`.

There is no source-side counterpart to iterate, so a source option that names an output is
not admitted through the same authority.

## Impact

Environment placeholders in source output-naming options are outside the admission check
that covers the equivalent transform options, and those keys are not covered by config
fingerprinting. This is the remaining half of a gap whose transform side was closed
earlier; the source half was left open deliberately, not overlooked.

## Fix

Correct behaviour is that both plugin kinds declare their output-naming options explicitly
and the loader consults whichever declaration applies, with no `kind`-specific gap. Work:

1. Inventory the source plugins that expose output-name options.
2. Add a closed declaration on the source base class equivalent to
   `output_naming_config_keys`, so each source states its own keys.
3. Remove the transform-only gate in `config_loading.py` so the consult covers both, and
   extend fingerprinting to the newly declared keys.

The declaration must be per-plugin and explicit. Inferring these options by matching
option names against a heuristic is not acceptable — that is precisely what the transform
side avoided by declaring them.

You would know it holds when a source option carrying a `${VAR}` placeholder that names an
output field is refused or admitted by the same rule that governs the equivalent transform
option, with a test covering a declared key and an undeclared one.

Size and what needs deciding first: this is the largest of its family. It needs a source
contract design — deciding what the source-side declaration looks like and how it is
enforced — and a registry migration to carry existing source plugins onto it. Do not pick
this up expecting a small patch; the design decision comes before any code.
