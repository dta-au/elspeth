# Single-Row LLM Source

This example starts a pipeline with the source-native `llm` plugin. It sends
one authored prompt to a local OpenAI-compatible ChaosLLM server and, on a
successful response, writes one generated row to JSONL. There are no input
rows and no LLM transform.

```text
llm source (one prompt) ──> result (one row)
```

## Validate Offline

Configuration validation does not start the provider, perform provider
preflight, or send a request. It therefore needs neither ChaosLLM nor a
fingerprinting key:

```bash
elspeth validate --settings examples/llm_source/settings.yaml
```

## Run Against ChaosLLM

The example uses ChaosLLM's `silent` preset so its single request follows a
bounded zero-error happy path. Start the server in one terminal:

```bash
.venv/bin/chaosllm serve --port 8199 --preset=silent --workers=1
```

Then establish a process-scoped fingerprint key and run the pipeline from
another terminal:

```bash
source examples/chaosllm_env.sh
elspeth run --settings examples/llm_source/settings.yaml --execute
```

`ELSPETH_FINGERPRINT_KEY`, established by the shared environment helper,
protects the audit fingerprint of the fake inline token. The token is not an
OpenRouter credential, and the configured endpoint is the local loopback
server.

The successful request writes one row to
`examples/llm_source/output/briefing.jsonl` with these fields:

| Field | Meaning |
|-------|---------|
| `briefing` | Generated text, using the example's custom `response_field` |
| `briefing_usage` | Token counts reported by ChaosLLM |
| `briefing_model` | Model identifier reported by ChaosLLM |

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
