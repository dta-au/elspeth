---
title: Composer Stage-1 construction probes may fail for operator-profiled plugins beyond llm
labels: [area/composer, type/bug]
---

Before validating a draft pipeline, the composer tries constructing each plugin from the
options the author wrote. For plugins whose configuration is completed by an operator
profile, that construction can fail every time rather than only for a malformed draft —
and for one class of plugin the fail-closed path then votes "participates, guarantees
nothing", which wrongly rejects any downstream node that requires fields.

The probe lives at `src/elspeth/web/composer/_validation_probe.py`; the vote logic it
feeds is in `src/elspeth/web/composer/state.py`.

## Why

The probe path is `prepare_validation_probe_options`
(`src/elspeth/web/composer/_validation_probe.py:24`) feeding `create_transform`
(`src/elspeth/plugins/infrastructure/manager.py:267`).

An operator profile is a server-side configuration block that fills in the parts of a
plugin's options an author must not write — provider bindings, endpoints, credentials.
It is applied by `lower_options` in `src/elspeth/web/plugin_policy/profiles.py`. The probe
is given only the authored options, so for these plugins the constructor is called without
config it requires, and fails.

The consequence splits by plugin class:

- **Pass-through plugins** — those declaring `passes_through_input=True`, meaning they
  emit the row they were given rather than replacing it. The fail-closed arm of
  `_effective_producer_vote` (`src/elspeth/web/composer/state.py:4925`) returns
  `participates=True` with an empty guarantee set. A downstream node that requires a field
  then sees a producer promising nothing, and validation rejects a pipeline that would
  have run.
- **Non-pass-through plugins** — the fallback is the plugin's raw schema declarations.
  Degraded, because there is no computed contract, but honest.

This mechanism is established and was measured for the llm plugin, which has its own
handling: `5d48f67c0` injects an inert gateway stub so the probe can construct. This issue
covers the remaining operator-profiled plugins.

## Impact

Profile resolvers exist for at least LLM profiles, Bedrock guardrail profiles
(`aws_bedrock_content_safety`, `aws_bedrock_prompt_shield`), Textract profiles and the S3
source. Several are pass-through, where the false-reject arm applies:
`aws_bedrock_content_safety`, `aws_bedrock_prompt_shield`,
`aws_textract_document_analysis`, `aws_textract_inline_analysis`, `azure_content_safety`,
`azure_prompt_shield`, `azure_document_intelligence`, `rag_retrieval`, `web_scrape` and
others.

No live report implicates any of these plugins yet. This is a declared residue of a
measured llm-specific defect, not confirmed breakage in the others: some may construct
without their profile block, and that has not been checked.

## Fix

Start by measuring, not patching. The first deliverable is a table: for each
operator-profiled plugin, does a Stage-1 probe from typical composer-authored options
construct or fail? That table is useful on its own and decides how much of the rest is
needed.

For each plugin that fails, the correct behaviour depends on a property you have to check
per plugin:

- If the plugin's computed output contract is provably independent of the profile-injected
  fields, extend the stub injection in `prepare_validation_probe_options` with an inert,
  provider-independent stub, as was done for llm. The `plugin` keyword is already required
  at every call site, so the hook exists.
- If the contract is not independent of the injected config, no stub is honest, and the
  vote shape has to be decided instead. Abstention rather than participate-with-empty is
  the candidate, but note that `_parse_producer_guarantees`
  (`src/elspeth/web/composer/state.py:6098`) folds abstention to empty in the node
  direction, so abstaining alone does not remove the false rejects. That is an open design
  question, not a settled approach.

You would know a per-plugin fix holds when a pipeline placing that plugin upstream of a
required-fields consumer validates successfully, and when the probe's computed guarantees
match what the plugin actually writes at runtime.

Size and readiness: the measurement step is small and can be picked up cold. The fixes
after it are per-plugin and need knowledge of each provider's endpoints, regions and
capability sets; the second branch above needs a design decision before any code.
