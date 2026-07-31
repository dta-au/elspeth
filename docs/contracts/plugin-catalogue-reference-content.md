# Plugin catalogue reference content

## Scope

The live `PluginManager` registry is the inventory authority for built-in
plugins. Catalogue reference content belongs on each plugin class beside its
executable contract; there is no separate production manifest. Gates and other
structural graph nodes are system operations and are outside this contract.

The catalogue response fields remain optional so third-party and legacy plugins
can continue to load. Repository tests require every registered built-in,
including the resume-only `null` source, to provide complete content.

Reference content documents current behavior. Adding or correcting it does not
change runtime behavior, policy, or `plugin_version`. Editing a plugin module
does change its `source_file_hash`, which must be refreshed.

## Authoring contract

Every registered built-in defines:

- `usage_when_to_use`: nonblank prose naming a concrete input or workflow, the
  useful outcome, and any decisive operating context;
- `usage_when_not_to_use`: nonblank prose naming a hard limitation or unsafe
  fit and a concrete alternative where one exists;
- `example_use`: one bounded, parseable YAML component fragment using real
  option names and realistic non-secret values; and
- `capability_tags`: a tuple of 2–6 unique lowercase kebab-case discovery
  terms, each no longer than 32 characters and with at least one
  plugin-specific term.

Use and Avoid prose must be specific and distinct, not copies of the technical
description or generic directions to consult other documentation. State
present implementation truth, not roadmap behavior. Distinguish ordinary Web
Composer use, operator-profiled configuration, and CLI/batch-only use whenever
that distinction changes whether the plugin is selectable or usable.

Describe boundedness, resume or append behavior, retained external-call
results, failure routing, audit consequences, and aggregation-window semantics
when those facts affect selection. Do not invent plugin IDs, options,
credentials, or deployed endpoints, and do not claim statistical significance
that the implementation does not compute.

The `narrative-summary` tag is a rendering contract, not a general discovery
synonym. Preserve it on `batch_classifier_metrics` and
`batch_distribution_profile`.

## YAML component fragments

An example contains exactly one occurrence of its declaring plugin. It is a
readable component fragment, not a complete runnable pipeline: connection and
routing validation belongs to settings tests. Sources, sinks, and aggregations
may use the repository's mapping or list form.

A source belongs under top-level `sources`:

```yaml
sources:
  orders:
    plugin: csv
    options:
      path: data/orders.csv
      schema: {mode: observed}
      on_validation_failure: discard
```

An ordinary row transform belongs directly under top-level `transform`:

```yaml
transform:
  plugin: passthrough
  options:
    schema: {mode: observed}
```

A batch-aware transform belongs under top-level `aggregations`:

```yaml
aggregations:
  - name: order_totals
    plugin: batch_stats
    options:
      schema: {mode: observed}
      value_field: amount
      compute_mean: true
```

A sink belongs under top-level `sinks`:

```yaml
sinks:
  results:
    plugin: json
    options:
      path: output/results.jsonl
      format: jsonl
      schema: {mode: observed}
```

The `null` source is not exempt. Its example must quote the plugin ID so YAML
does not parse it as a null scalar, and it has no options:

```yaml
sources:
  resume_placeholder:
    plugin: "null"
```

## Credentials

Never put a credential literal in catalogue YAML. When a user-configurable
credential field is material to the example, use an exact deferred marker with
a nonblank inventory name:

```yaml
api_key:
  secret_ref: PROVIDER_API_KEY
```

Place the marker only in a credential-bearing option. Operator-profiled plugins
should demonstrate their profile/configuration selector instead of embedding
credentials.

The catalogue test kit checks the unmodified options for literal credentials
and disallowed marker placement. Only after those checks pass does it replace
an exact `{secret_ref: NAME}` mapping with a fixed non-secret sentinel for the
owning config model. It never resolves the reference, expands environment
variables, reads process environment, constructs a plugin, or makes an external
call.

## Validation and source hashes

Use the reusable assertions in `tests/fixtures/catalog_reference.py`.
`parse_and_validate_example` parses through `load_bounded_pipeline_yaml`,
requires the declaring plugin exactly once in the correct section, and calls
the class's `get_config_model(options)` followed by
`from_dict(options, plugin_name=plugin_cls.name)`.

After editing one plugin module, refresh its hash mechanically:

```bash
PYTHONPATH=src .venv/bin/python - \
  src/elspeth/plugins/<family>/<module>.py PluginClass <<'PY'
import sys
from pathlib import Path

from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash

path = Path(sys.argv[1])
fix_source_file_hash(path, sys.argv[2], compute_source_file_hash(path))
PY
```

Run the focused helper and family tests while authoring:

```bash
PYTHONPATH=src .venv/bin/pytest \
  tests/unit/plugins/test_catalog_reference_testkit.py \
  tests/unit/plugins/<family>/test_<family>_catalogue_metadata.py -q
```

Then run the global repository gate:

```bash
PYTHONPATH=src .venv/bin/pytest tests/
```

For plugin-module edits, also run the hash rule:

```bash
PYTHONPATH=elspeth-lints/src .venv/bin/python -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes src/elspeth
```

## Future-author checklist

- Define all four fields on the plugin class; inherited empty defaults fail the
  registry-driven built-in gate.
- Make Use concrete about input/workflow, outcome, and decisive context.
- Make Avoid concrete about a hard boundary and the safer or better
  alternative.
- Use 2–6 unique lowercase kebab-case tags, preserving any rendering-contract
  tag such as `narrative-summary`.
- Put one declaring-plugin node in the correct bounded YAML section.
- Use only real options and realistic, non-secret values.
- Use exact secret-ref markers only in credential fields; never use credential
  literals.
- Record material Web/operator/CLI, boundedness, resume, audit, failure, and
  window semantics.
- Refresh `source_file_hash`, run the family tests, and finish with the global
  repository test suite.
