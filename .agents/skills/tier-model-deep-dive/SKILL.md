---
name: tier-model-deep-dive
description: >
  Detailed trust tier examples and rules for writing plugins — external call boundaries
  in transforms, pipeline template error handling, coercion rules by plugin type, and
  operation wrapping rules. Use when writing or modifying sources, transforms, sinks,
  or any plugin code that handles data at trust boundaries.
---

# Three-Tier Trust Model — Deep Reference

This skill provides detailed code examples, tables, and boundary rules for ELSPETH's
three-tier trust model. For the core rules (what each tier means), see CLAUDE.md.

## External Call Boundaries in Transforms

**CRITICAL:** Trust tiers are about **data flows**, not plugin types. **Any data crossing from an external system is Tier 3**, regardless of which plugin makes the call.

Transforms that make external calls (LLM APIs, HTTP requests, database queries) create **mini Tier 3 boundaries** within their implementation:

```python
def process(self, row: PipelineRow, ctx: PluginContext) -> TransformResult:
    # row enters as Tier 2 (pipeline data - trust the schema)

    # 1. External call creates Tier 3 boundary — wrap it
    try:
        llm_response = self._llm_client.query(prompt)  # EXTERNAL DATA - zero trust
    except Exception as e:
        return TransformResult.error({"reason": "llm_call_failed", "error": str(e)}, retryable=True)

    # 2. IMMEDIATELY validate at the boundary — retryable=False (bad data, not transient)
    try:
        parsed = json.loads(llm_response.content)
    except json.JSONDecodeError:
        return TransformResult.error({"reason": "invalid_json", "raw": llm_response.content[:200]}, retryable=False)

    if not isinstance(parsed, dict):
        return TransformResult.error({"reason": "invalid_json_type", "expected": "dict", "actual": type(parsed).__name__}, retryable=False)

    # 3. NOW it's our data (Tier 2) — validated, trust it
    output = {**row.to_dict(), "llm_classification": parsed["category"]}
    return TransformResult.success(output, success_reason={"action": "llm_classified"})
```

**The rule: Minimize the distance external data travels before you validate it.**

- Validate immediately — right after the external call returns
- Coerce once — normalize types at the boundary
- Trust thereafter — once validated, it's Tier 2 pipeline data
- Don't carry raw external data — passing `llm_response` to helper methods without validation
- Don't defer validation — "I'll check it later when I use it"
- Don't validate multiple times — if it's validated once, trust it

**Common external boundaries in transforms:**

| External Call Type | Tier 3 Boundary | Validation Pattern |
|-------------------|-----------------|-------------------|
| LLM API response | Response content | Wrap JSON parse, validate type is dict, check required fields |
| HTTP API response | Response body | Wrap request, validate status code, parse and validate schema |
| Database query results | Result rows | Validate row structure, handle missing fields, coerce types |
| File reads (in transform) | File contents | Same validation as source plugins |
| Message queue consume | Message payload | Parse format, validate schema, quarantine malformed messages |

## Coercion Rules by Plugin Type

| Plugin Type | Coercion Allowed? | Rationale |
|-------------|-------------------|-----------|
| **Source** | Yes | Normalizes external data at ingestion boundary |
| **Transform (on row)** | No | Receives validated data; wrong types = upstream bug |
| **Transform (on external call)** | Yes | External response is Tier 3 - validate/coerce immediately |
| **Sink** | No | Receives validated data; wrong types = upstream bug |

## Operation Wrapping Rules

| What You're Accessing | Wrap in try/except? | Why |
|----------------------|---------------------|-----|
| `self._config.field` | No | Our code, our config - crash on bug |
| `self._internal_state` | No | Our code - crash on bug |
| `landscape.get_row_state(token_id)` | No | Our data - crash on corruption |
| `checkpoint_data["tokens"]` | No | Our data - we wrote this JSON |
| `row["field"]` arithmetic/parsing | Yes | Their data values can fail operations |
| `external_api.call(row["id"])` | Yes | External system, anything can happen |
| `json.loads(external_response)` | Yes | External data - validate immediately |
| `validated_dict["field"]` | No | Already validated at boundary - trust it |

**Rule of thumb:**

- **Reading from Landscape tables?** Crash on any anomaly - it's our data.
- **Reading checkpoints or deserialized audit JSON?** Crash on any anomaly - it's our data.
- **Operating on row field values?** Wrap operations, return error result, quarantine row.
- **Calling external systems?** Wrap call AND validate response immediately at boundary.
- **Using already-validated external data?** Trust it - no defensive `.get()` needed.
- **Accessing internal state?** Let it crash - that's a bug to fix.

**Serialization does not change trust tier.** Data we wrote to our own database or checkpoint file is still Tier 1 when we read it back, even though it passes through `json.loads()` or SQLAlchemy deserialization. The trust boundary is about *who authored the data*, not the transport format. Checkpoints, audit records, and Landscape tables are all our data — we defined the schema, we wrote the values, we own the invariants. If a deserialized checkpoint is missing a `"tokens"` key or a `"row_id"` field, that is corruption in our system, not a data quality issue to handle gracefully. Crash immediately.

## Pipeline Templates as Tier 2 Data

Pipeline templates (Jinja2 prompt templates in YAML config) are **Tier 2 — user-provided, validated at load time, trusted during rendering**. This creates two distinct error categories:

| Failure Type | When | Effect | Example |
|-------------|------|--------|---------|
| **Structural** (parse) | Init time | Stop run — no row will ever succeed | `{% if unclosed` (syntax error) |
| **Operational** (render) | Per-row | Quarantine row, continue pipeline | `{{ row.missing_field }}` (undefined variable) |

**Rules:**

- Templates are parsed **once** at plugin construction (`__init__` / `__post_init__`), never re-parsed per-row
- `TemplateSyntaxError` during parse -> `TemplateError` propagates up -> run fails at setup
- `UndefinedError` / `SecurityError` during render -> `TemplateError` caught by transform -> `TransformResult.error()` -> row quarantined
- Per-query template overrides (multi-query LLM transforms) are pre-compiled in `MultiQueryStrategy.__post_init__`, not deferred to first row

**Why this matters:** A broken template can never produce valid results for any row. Deferring the parse error to render time would misclassify a config error (structural) as a data error (operational), quarantining the first row instead of stopping the run.

## Tier 2 Nuance: Type-Safe != Operation-Safe

```python
# Data is type-valid (int), but operation fails
row = {"divisor": 0}  # Passed source validation
result = 100 / row["divisor"]  # ZeroDivisionError - wrap this!

# Data is type-valid (str), but content is problematic
row = {"date": "not-a-date"}  # Passed as str
parsed = datetime.fromisoformat(row["date"])  # ValueError - wrap this!
```
