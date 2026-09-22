---
title: Plugin source-hash gate skips plugins/transforms/aws, leaving four transforms ungated
labels: [area/plugins, type/bug]
---

The lint that pins each plugin's source identity does not scan
`plugins/transforms/aws`, while plugin discovery does. Every AWS transform is therefore
registered and usable but never hash-gated: changing one produces no stale-hash finding,
so the gate is inert for that directory.

This work is in the lint package, which is a separate source root from the runtime:
`elspeth-lints/src/elspeth_lints/rules/plugin_contract/plugin_hashes/rule.py`, run via
`elspeth-lints check`. The rule records a hash of each plugin's source file and reports a
finding (rule code PH3) when a file's current content no longer matches its recorded hash.

## Why

The gate's directory list, `rule.py:23-31`:

```python
PLUGIN_DIRS = (
    "plugins/sources",
    "plugins/sources/llm",
    "plugins/sinks",
    "plugins/transforms",
    "plugins/transforms/azure",
    "plugins/transforms/llm",
    "plugins/transforms/rag",
)
```

The runtime's discovery authority, `src/elspeth/plugins/infrastructure/discovery.py:287-291`:

```python
PLUGIN_SCAN_CONFIG: dict[str, list[str]] = {
    "sources": ["sources", "sources/llm"],
    "transforms": ["transforms", "transforms/aws", "transforms/azure", "transforms/llm", "transforms/rag"],
    "sinks": ["sinks"],
}
```

`transforms/aws` is in the scan config and absent from the gate. Its direct sibling
`azure` — same shape, same commit era — is listed, and there is no `EXCLUDED_FILES` entry
recording an intentional skip, which is what makes this an omission rather than a
deliberate exclusion.

The rule's own discovery guard cannot catch it. The message at `rule.py:98`
(`DISCOVERY ERROR: found {plugin_count} plugins, expected at least {min_plugins}`) is a
floor check, and the count stays above the floor with the AWS directory missing.

## Impact

Four registered transforms under `src/elspeth/plugins/transforms/aws/` are affected:
`aws_bedrock_content_safety`, `aws_bedrock_prompt_shield`,
`aws_textract_document_analysis` and `aws_textract_inline_analysis`. All four appear in
the live catalogue (the composer's `list_transforms` tool reports 34 available).

This is not a low-assurance corner. `textract_result.py` and `textract_bucket_region.py`
both carry `@trust_boundary` decorators, marking them as parsing boundaries for data the
project does not control — exactly the code whose source identity most needs pinning.

## Fix

Correct behaviour is that the set of directories the gate scans cannot disagree with the
set discovery scans. The tempting change — adding `"plugins/transforms/aws",` as an eighth
tuple entry — reproduces the cause: `PLUGIN_DIRS` is a hand-maintained restatement of
`PLUGIN_SCAN_CONFIG`, so the two drift whenever a plugin subdirectory is added, and the
drift is silent in the direction that weakens the gate. Deriving `PLUGIN_DIRS` from
`PLUGIN_SCAN_CONFIG`, prefixing each entry with `plugins/`, gates a new subdirectory the
moment discovery can see it and inverts the failure mode: an unresolvable directory
becomes an error rather than a silent skip.

You would know it holds by making the gate go red on a change it currently misses:

- Before: edit any plugin class body under `src/elspeth/plugins/transforms/aws/`, then run
  `pytest tests/unit/elspeth_lints/test_plugin_contract_rules.py::test_plugin_hashes_json_mode_succeeds_on_current_codebase`
  — expect it to pass despite a now-stale hash.
- After: the same edit must produce a PH3 stale-hash finding naming the file.

Size and what needs deciding first: small in lines, but the derivation makes the lint
package import the runtime's discovery config, and `elspeth_lints` currently imports
nothing from `elspeth` (measured: no such import exists anywhere under
`elspeth-lints/src/`). Whether that dependency direction is acceptable is a decision to
settle before writing the patch. The eighth-tuple-entry route is the genuinely one-line
fallback if it is not.

The four AWS transforms will also need a one-shot `source_file_hash` bootstrap once they
enter the gate; expect their declared values to be wrong or absent today, since nothing
has ever checked them.
