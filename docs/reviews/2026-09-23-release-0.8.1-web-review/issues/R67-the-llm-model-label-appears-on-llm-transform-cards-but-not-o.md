# R67. The LLM model label appears on llm transform cards but not on llm source cards

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-08#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx:1185-1197,1213-1215,2051-2053`.
- **Wrong:** daad04b0b gates the label on `node_type === 'transform'`, but the `llm` source plugin has a `model` field. The Wiring table already labels both node types.
- **Fix:** Pass `llmBindingLabel` for `llm` sources, and add a test case.
- **Sources:** fe-08#1.


## Source findings and verification

### fe-08#1: LLM model label shown on llm transform cards but not on llm source cards

- **Reported at:** `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx:1213`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** daad04b0b adds the model/profile to the node card and accessible description only when node_type === 'transform' && plugin === 'llm'. The source loop (1185-1197) and the source branch of the a11y describe loop pass no model label, but there is an LLM source plugin named 'llm' whose config has a model field.
- **Failure scenario:** A pipeline starts with an llm source (model anthropic/claude-sonnet-4) that feeds an llm transform (model gpt-4o). The transform card reads 'llm · model gpt-4o'. The source card reads just 'llm', and its a11y list entry has no model. So the canvas does not show which model generates the rows, although the collapsed Wiring table does, because pluginBindingLabel dispatches on plugin === 'llm' whatever the node kind.
- **Evidence:** GraphView.tsx:1213-1215 and 2051-2053 gate on node_type === 'transform'. src/elspeth/plugins/sources/llm/source.py:122 has name = 'llm'. sources/llm/config.py:61 has model: str | None. lib/pluginBindingLabel.ts:8 calls llmBindingLabel for any plugin === 'llm'. The GraphView.test.tsx it.each added in daad04b0b covers only the transform case.
- **Suggested fix:** In the source loop, pass source.plugin === 'llm' ? llmBindingLabel(source.options) : undefined as modelLabel to makeRfNode, append the same label in the source describe branch, and add a source case to the test.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. At 74c0ce0db the model label reaches the card only for transform nodes. The source loop never passes the new `modelLabel` argument, and the accessible-description loop for sources doesn't add it either. The LLM source plugin is a real plugin that the web path uses and assigns an operator profile, and its options include a `model` field. The frontend `SourceSpec` carries the options, and the Wiring table already labels sources through `pluginBindingLabel`. So an llm source card shows only 'llm', with no model or profile, while an llm transform card shows 'llm · model X'. The commit title is 'show LLM model in graph nodes', with no transform-only qualifier, and I found no recorded ruling that leaves sources out. This is cosmetic and informational, so the severity stays low.
