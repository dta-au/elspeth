# Single-Row LLM Source

This example starts a pipeline with the source-native `llm` plugin. It sends
one authored prompt to OpenRouter and, on a successful response, writes one
generated row to JSONL. There are no input rows and no LLM transform.

```text
llm source (one prompt) ──> result (one row)
```

## Prerequisites

Create an OpenRouter API key and expose it to ELSPETH:

```bash
export OPENROUTER_API_KEY="your-key-from-openrouter.ai"
```

## Validate Without Calling OpenRouter

Configuration validation expands the environment reference but does not run
provider preflight or send a request:

```bash
elspeth validate --settings examples/llm_source/settings.yaml
```

## Run

The pipeline makes exactly one OpenRouter request. The shell timeout is a
cost-control guard for the example:

```bash
timeout 30 elspeth run --settings examples/llm_source/settings.yaml --execute
```

The successful request writes one row to
`examples/llm_source/output/briefing.jsonl` with these fields:

| Field | Meaning |
|-------|---------|
| `briefing` | Generated text, using the example's custom `response_field` |
| `briefing_usage` | Token counts reported by OpenRouter |
| `briefing_model` | Model identifier reported by OpenRouter |

`briefing_usage` mirrors the provider report. ELSPETH does not invent zero
counts for values a provider omits. The exact generated text and token counts
can vary between runs.

The source uses `on_validation_failure: discard` deliberately to keep this
example to one success path and one output sink. In a production pipeline,
route invalid generated rows to a quarantine sink when they must remain
inspectable.

## Content Safety Policy

This local CLI example assumes Content Safety is in `recommend` mode. A
deployment that requires Content Safety must place its real selected control
downstream of the LLM source before the sink. Do not add a placeholder control
merely to make the example appear covered.
