# Transforms — Pipeline Data vs Audit Provenance

## Wrongly-typed row values: return, never raise

A row value of the wrong type is row DATA, not a bug to crash on. Under an
`observed` schema, or a field the schema leaves untyped (`any`), nothing
upstream checked it.

- **Never coerce it.** `"42"` stays a wrong type; do not turn it into `42`.
- **Never raise on it.** A bare `TypeError` (or `KeyError`, `ValueError`)
  escaping `process()` matches no conversion in the engine: the run aborts with
  a traceback and no terminal outcomes (elspeth-5887fb7928).
- **Return `TransformResult.error(..., retryable=False)`.** A row-level
  transform's row then leaves through its `on_error`. At a batch node one bad
  row fails the WHOLE batch: every buffered row routes via the aggregation's
  `on_error`, and a collector fails its group.
- **Never put the value in the reason.** Name the field, the expected and
  found type, and the (batch) row index.
- **In a batch helper that returns a value,** raise `BatchRowTypeError` from
  `_batch_row_types.py` and catch it once in `process()`:
  `return TransformResult.error(exc.as_reason(), retryable=False)`
  (`batch_threshold_summary.py` is the pattern). Use `type(x) not in (...)`,
  not `isinstance`: `bool` is an `int`.

`tests/unit/plugins/test_process_path_type_error_gate.py` fails the build on an
explicit `raise TypeError` that a class's own `process` reaches through its
methods, same-module bases and module functions. It does not follow composed
helper objects (`self._builder.build(...)`) or code in another module, and it
never roots a transform that inherits `process` from another module (the RAG,
Azure and Bedrock families here), so it cannot vouch for those. Its escape
hatch is a reviewed entry in that file, never a suppression.

## The Decision Test

> Would a pipeline operator or downstream transform ever make a decision based on this value?

- **Yes** → Output row field (via `declared_output_fields`)
- **No** → Audit trail (via `success_reason["metadata"]`)

## Examples

| Field | Location | Why |
|-------|----------|-----|
| `fetch_status = 403` | Row | A gate might route forbidden pages to review |
| `llm_response_model = "gpt-4"` | Row | Multi-model routing, cost filtering |
| `llm_response_usage` | Row | Budget tracking, cost routing |
| `template_hash = abc123` | Audit | Forensic reconstruction only |
| `variables_hash` | Audit | Forensic reconstruction only |
| `fetch_request_hash` | Audit | Blob reference for forensic recovery |

## Where Audit Provenance Goes

```python
return TransformResult.success(
    PipelineRow(output, contract),
    success_reason={
        "action": "enriched",
        "metadata": {
            "template_hash": rendered.template_hash,
            "variables_hash": rendered.variables_hash,
            # ... other provenance fields
        },
    },
)
```

Persisted in `node_states.success_reason_json`. Retrievable via `elspeth explain`.

## Blob Storage

Transforms that produce processed content (not from an external call) store blobs
via `recorder.store_payload(content, purpose="descriptive_label")`.

Request/response blobs from external calls are already stored by `AuditedHTTPClient`
via `recorder.record_call()`. Access hashes from the returned `Call` object:
`call.request_ref`, `call.response_ref`.

**Scope constraint:** If `recorder.store_payload()` appears in more than 2-3
transforms, the Shifting the Burden archetype is reforming and the design needs
revisiting.

## Constants

Each transform with audit-only fields defines a constant tuple:
- `LLM_AUDIT_SUFFIXES` in `plugins/transforms/llm/__init__.py`
- `WEBSCRAPE_AUDIT_FIELDS` in `plugins/transforms/web_scrape.py`
