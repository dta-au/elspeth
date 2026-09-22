---
title: Aggregate plugin contract budget overflows and the planner's third schema call is refused
labels: [area/composer, type/bug]
---

The planner's aggregate selected-schema contract total measured 50,365 bytes against a 49,152-byte budget. Because the budget is enforced incrementally, the third `get_plugin_schema` call in an ordinary three-plugin selection is refused, and the planner proceeds without knowing what that plugin accepts.

## What happens

The Composer is ELSPETH's web authoring interface. Its *planner* is the LLM loop that authors a pipeline proposal, and it learns what a plugin accepts through *discovery calls* — tool calls returning that plugin's config contract.

With `web_scrape`, `llm` and `field_mapper` selected, the first two contracts are served and the third discovery call returns an error payload in place of `{plugin_id, schema_hash, json_schema, knob_schema, composer_hints}`:

```json
{"error": "The selected plugin contracts exceed the aggregate planner schema budget. Use get_plugin_assistance.",
 "error_code": "schema_contract_budget_exceeded",
 "next_tool": "get_plugin_assistance"}
```

The same path records that fact as unavailable in the information manifest, the planner's ledger of what it has successfully read. The planner does not crash and is handed a named fallback tool, so the degradation is graceful — but it plans against a contract it never read.

## Why

Start in `src/elspeth/web/composer/pipeline_planner.py`. The constant `_SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES` is `48 * 1024` = 49,152 bytes and governs the **aggregate** of all selected contracts, not any single one. Before each discovery call the planner computes a running remainder:

```python
encoded_contracts = len(canonical_json(selected_schema_contracts).encode("utf-8"))
budget_remaining = _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES - encoded_contracts - (1 if selected_schema_contracts else 0)
```

Measured:

```
BUDGET (aggregate)     49,152 bytes
ACTUAL (aggregate)     50,365 bytes
OVER BY                 1,213 bytes

  web_scrape            5,708
  llm                  39,095   <- 79.5% of the budget, one plugin
  field_mapper          5,564
```

No single contract exceeds the budget; 39,095 fits inside 49,152 with room. What overflows is the aggregate, and one plugin taking four fifths of a shared budget is what makes an ordinary three-plugin selection overflow it. About 10,057 bytes remain once `llm` is selected, so only one further contract of roughly 5.5 KB fits alongside it. Both accumulator sites sit inside `_plan_pipeline_inner`, so the budget resets per plan attempt rather than accumulating across a session.

A contract has three parts: `json_schema` (the config the plugin accepts), `knob_schema` (the same options flattened into the per-option surface the planner is shown) and `composer_hints`. Running the two failing tests either side of the change pins the growth:

```
1f60adb64   aggregate 48,207   under by   945   ->  2 passed
2b452ff8c   aggregate 50,365   over  by 1,213   ->  2 failed
```

`2b452ff8c feat(llm): structured output on every LLM plugin surface` lifted the multi-query `response_format` / `output_fields` pair to the `llm` transform's config top level:

| component | 1f60adb64 | 2b452ff8c | delta |
|---|---|---|---|
| `json_schema` | 11,698 | 12,526 | +828 |
| `knob_schema` | 19,040 | 20,370 | +1,330 |
| `composer_hints` | 6,039 | 6,039 | 0 |
| contract total | 36,935 | 39,093 | +2,158 |

(Components exclude canonical-JSON framing, so they do not sum exactly to the totals.) `$defs` are pydantic-deduplicated — `OutputFieldConfig` appears once, at 254 bytes — so the `json_schema` growth is honest. With a pre-existing margin of 945 bytes, a +2,158-byte contract was always going to overflow.

## Failing tests

- `tests/unit/web/composer/test_planner_authoring_aids.py::TestDiscoveryDigest::test_three_selected_plugin_contracts_fit_the_canonical_utf8_budget`
- `tests/unit/web/composer/test_pipeline_planner.py::test_generic_linear_plan_reuses_initial_information_and_needs_one_discovery_turn`

These are one defect, not two. The second is named for a turn count, but every turn-count assertion in it passes (`len(completion.requests) == 2`, three tool invocations, `repair_count == 0`, correct nodes). It fails later, on the shape of the third tool response — triaging it as a planner-efficiency problem will chase the wrong thing.

## Impact

Any plan attempt selecting `llm` plus roughly two other plugins loses one contract read. `web_scrape -> llm -> field_mapper` is also the shape the first-run tutorial authors, which Architecture Decision Record 031 (`docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md`) designates a fixed-script canary.

No live tutorial break is claimed: `test_tutorial_service.py` and `test_tutorial_telemetry.py` pass (27 tests), as does the tutorial-shape case in `test_set_pipeline_candidate.py`. **Whether a live run reaches the third `get_plugin_schema` call at all is undetermined** — settling that needs a live run against a real model provider, which the read-only investigation behind this report could not do. The green tutorial suites are equally not an all-clear.

## Fix

Two sizes of change, and they are not the same job.

**The stopgap is one line**: raise `_SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES`. It unblocks immediately but loosens a limit with only a 945-byte margin, so the next pair of options breaks it again. If taken, it should be explicit and temporary.

**The real fix is medium-sized and needs a measurement before anyone writes code.** Shrink the `llm` contract instead. `knob_schema` is 20,370 bytes — 52% of that contract and the larger half of the growth. Being the flattened surface, it is the half most likely to carry the same option twice: at the top level and again nested under `queries[]`. That is a lead, not a verified finding. Confirm or refute the double-carry first. The flattening happens in `src/elspeth/web/catalog/knob_schema.py`, which lowers a plugin's pydantic config model into knob fields; the models concerned are in `src/elspeth/plugins/transforms/llm/` (`multi_query.py` holds `QueryDefinition` and `OutputFieldConfig`). If the duplication is not real, say so on the issue — the contract then needs trimming another way.

Done looks like: both named tests pass with the budget constant unchanged, and the canonical UTF-8 encoding of the three-plugin selection measures under 49,152 bytes with room for a fourth plugin.

Two things not to do. Making discovery degrade per plugin is not the missing piece — it already does. And `test_planner_authoring_aids.py` now imports the budget constant instead of restating `48 * 1024`, so raising the constant will not turn it green; it is expected to stay red until the contract shrinks.
