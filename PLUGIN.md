# ELSPETH Plugin Development Guide

Create custom sources, transforms, and sinks for ELSPETH pipelines.

> **Quick Links:**
>
> - [Example transform](#example-transform) - Start with a small plugin
> - [Plugin Types](#plugin-types-overview) - Choose the right type
> - [Contract Tests](#contract-testing) - Verify your plugin works

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Example Transform](#example-transform)
- [Plugin Types Overview](#plugin-types-overview)
- [Creating Transforms](#creating-a-transform-plugin)
- [Creating Sources](#creating-a-source-plugin)
- [Creating Sinks](#creating-a-sink-plugin)
- [Plugin Registration](#plugin-registration)
- [Schema Configuration](#schema-configuration)
- [Contract Testing](#contract-testing)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Python 3.12+** with type hints, dataclasses
- **Pydantic v2** for config validation
- **ELSPETH concepts** - Read [Data Trust and Error Handling](docs/guides/data-trust-and-error-handling.md) for the Three-Tier Trust Model

```bash
git clone https://github.com/dta-au/elspeth.git
cd elspeth
uv sync --frozen --all-extras
source .venv/bin/activate
```

---

## Example Transform

This example sketches a row transform. Complete its contract tests and source
hash before registering it in a branch; the hash values shown throughout this
guide are placeholders, not gate-ready values.

```python
# src/elspeth/plugins/transforms/double_value.py
from typing import Any

from pydantic import Field

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult


class DoubleValueConfig(TransformDataConfig):
    """Config with custom field."""
    field: str = Field(default="value", description="Numeric field to double.")


class DoubleValueTransform(BaseTransform):
    """Double a numeric field value."""

    name = "double_value"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:0000000000000000"  # Replace before CI
    config_model = DoubleValueConfig

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = DoubleValueConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._field = cfg.field

        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "DoubleValue",
        )

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        if self._field not in row:
            return TransformResult.error({"reason": "missing_field", "field": self._field})

        # Under an observed schema the value's type is row data. Check it: never
        # coerce it, and never raise (a raise ends the run; a returned error
        # routes this one row). `"12.5" * 2` is "12.512.5", not a TypeError, so
        # catching TypeError around the operation would miss it. `type()`, not
        # isinstance: bool is an int. Name the field and type, never the value.
        value = row[self._field]
        if type(value) not in (int, float):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "error_type": "wrong_type",
                    "field": self._field,
                    "expected": "int or float",
                    "actual_type": type(value).__name__,
                },
                retryable=False,
            )
        result = value * 2

        output = row.to_dict()
        output[self._field] = result
        return TransformResult.success(
            PipelineRow(output, self._align_output_contract(row.contract)),
            success_reason={"action": "transformed", "fields_modified": [self._field]},
        )

    def close(self) -> None:
        pass
```

**Make it discoverable:**

Put the file under `src/elspeth/plugins/transforms/`. Built-in plugin discovery
scans that top-level directory automatically. If you add a new subdirectory,
add that subdirectory to `PLUGIN_SCAN_CONFIG` in
`src/elspeth/plugins/infrastructure/discovery.py`.

**Use it:**

```yaml
transforms:
- name: double_price
  plugin: double_value
  input: validated           # Explicit input connection
  on_success: doubled        # Named output connection
  on_error: discard          # Sink name for failed rows, or 'discard'
  options:
    schema:
      mode: observed
    field: price
```

**Test it:**

```python
# tests/unit/contracts/transform_contracts/test_double_value_contract.py
import pytest

from elspeth.plugins.transforms.double_value import DoubleValueTransform
from .test_transform_protocol import TransformContractPropertyTestBase

class TestDoubleValueContract(TransformContractPropertyTestBase):
    @pytest.fixture
    def transform(self):
        return DoubleValueTransform({"schema": {"mode": "observed"}, "field": "value"})

    @pytest.fixture
    def valid_input(self):
        return {"id": 1, "value": 10.0}
```

After formatting the new plugin file, compute and paste its actual source
hash, then run the contract test and source-hash gate. The hash computation
normalises the `source_file_hash` line itself, so replacing the placeholder
does not change the computed value:

```bash
.venv/bin/python -c "from pathlib import Path; from scripts.cicd.plugin_hash import compute_source_file_hash as h; print(h(Path('src/elspeth/plugins/transforms/double_value.py')))"
.venv/bin/python -m pytest tests/unit/contracts/transform_contracts/test_double_value_contract.py -n 0
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules plugin_contract.plugin_hashes --root src/elspeth
```

See the [whole-tree plugin gates](CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline)
for the required inventory updates when adding a built-in plugin.

---

## Plugin Types Overview

ELSPETH follows the **Sense/Decide/Act** model:

```
SOURCE (Sense) → TRANSFORM (Decide) → SINK (Act)
```

| Type | Purpose | Base Class | Key Method | Context |
|------|---------|------------|------------|---------|
| **Source** | Load data from external systems | `BaseSource` | `load()` | `SourceContext` |
| **Transform** | Process/classify rows | `BaseTransform` | `process()` | `TransformContext` |
| **Sink** | Publish output through the recoverable effect protocol | `BaseSink` | `prepare_effect()`, `commit_effect()`, `reconcile_effect()` | `RestrictedSinkEffectContext` |

### The Trust Model: Who Can Coerce Data?

| Plugin Type | Coercion Allowed? | Why |
|-------------|-------------------|-----|
| **Source** | ✅ Yes | External data boundary - normalize incoming data |
| **Transform** | ❌ No for pipeline row data; ✅ only for new external responses fetched by the transform | Pipeline data types are already validated; any HTTP/LLM/DB/file response is a fresh external boundary |
| **Sink** | ❌ No | Wrong types = upstream bug → crash |

**Rule:** Trust follows data flow, not plugin type. A transform that calls an
HTTP API, LLM, database, or file creates a new Tier 3 boundary inside the
transform: wrap the external call, validate/coerce the response immediately, and
then treat the validated result as pipeline data.

---

## Creating a Transform Plugin

Transforms process rows one at a time (or in batches for aggregation).

### Basic Transform

```python
from typing import Any

from pydantic import Field

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult


class MyTransformConfig(TransformDataConfig):
    """Config with your custom fields."""
    multiplier: int = Field(default=2, description="Factor to multiply the target field by.")
    target_field: str = Field(default="value", description="Numeric field to multiply.")


class MyTransform(BaseTransform):
    """Multiply a field by a configured factor."""

    name = "my_transform"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:0000000000000000"  # Replace before CI
    config_model = MyTransformConfig

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = MyTransformConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._multiplier = cfg.multiplier
        self._target_field = cfg.target_field

        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "MyTransform",
        )

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        if self._target_field not in row:
            return TransformResult.error({
                "reason": "missing_field",
                "field": self._target_field,
            })

        # A row value's type is row data under an observed schema: check it,
        # never coerce it, never raise. Catching TypeError around the operation
        # is not a check: `"12.5" * 2` succeeds and yields "12.512.5".
        value = row[self._target_field]
        if type(value) not in (int, float):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "error_type": "wrong_type",
                    "field": self._target_field,
                    "expected": "int or float",
                    "actual_type": type(value).__name__,
                },
                retryable=False,
            )
        # Wrap operations on row values that can still fail on a valid type
        # (division by zero, a date that does not parse) and return an error.
        result = value * self._multiplier

        output = row.to_dict()
        output[self._target_field] = result
        return TransformResult.success(
            PipelineRow(output, self._align_output_contract(row.contract)),
            success_reason={
                "action": "transformed",
                "fields_modified": [self._target_field],
            },
        )

    def close(self) -> None:
        pass
```

### Required Attributes

| Attribute | Type | Purpose |
|-----------|------|---------|
| `name` | `str` | Unique plugin identifier (class attribute) |
| `config_model` | `type[PluginConfig] \| None` | Pydantic config class rendered by `get_config_schema()` |
| `input_schema` | `type[PluginSchema]` | Expected input row schema |
| `output_schema` | `type[PluginSchema]` | Produced output row schema |
| `declared_input_fields` | `frozenset[str]` | Required input fields from `required_input_fields` |
| `declared_output_fields` | `frozenset[str]` | Fields added to every emitted row, used for collision checks |
| `_output_schema_config` | `SchemaConfig \| None` | Static output guarantee surface for DAG validation |
| `on_error` | `str \| None` | Sink name for error routing; injected by runtime settings |
| `on_success` | `str \| None` | Output connection name; injected by runtime settings |
| `determinism` | `Determinism` | Reproducibility level; every plugin MUST declare one in its own class body — there is no default, and an undeclared value raises `TypeError` at class creation |
| `plugin_version` | `str` | Plugin version for audit trail (default: `"0.0.0"`) |
| `source_file_hash` | `str \| None` | Entry-point file hash for audit identity; CI enforces concrete plugin values |
| `usage_when_to_use` | `str \| None` | Persona-facing prose naming a concrete input or workflow and the useful outcome |
| `usage_when_not_to_use` | `str \| None` | Persona-facing prose naming a hard limitation and a concrete alternative |
| `example_use` | `str \| None` | One bounded, parseable YAML component fragment using real option names |
| `capability_tags` | `tuple[str, ...]` | 2-6 unique lowercase kebab-case discovery terms, each at most 32 characters, at least one plugin-specific |

The last four are the catalogue reference content contract. The base defaults
stay optional so third-party and legacy plugins keep loading, but repository
tests require every registered built-in to provide all four. See
[Plugin catalogue reference content](docs/contracts/plugin-catalogue-reference-content.md).

**Determinism levels:**

- `DETERMINISTIC` - Same input always produces same output
- `SEEDED` - Capture the seed, replay with the same seed
- `IO_READ` - Reads from external source
- `IO_WRITE` - Writes to external sink
- `EXTERNAL_CALL` - Calls external service (LLM, API)
- `NON_DETERMINISTIC` - Must record output, cannot reproduce

### TransformResult Options

```python
# Success - transformed row (success_reason is REQUIRED)
TransformResult.success(
    PipelineRow(output_dict, output_contract),
    success_reason={"action": "classified"},
)

# Error - row failed processing (routes to on_error sink)
TransformResult.error({"reason": "invalid_input"})

# Multiple outputs (requires creates_tokens=True)
TransformResult.success_multi(
    [PipelineRow(row1, output_contract), PipelineRow(row2, output_contract)],
    success_reason={"action": "split"},
)

# Intentional zero emission for filters
TransformResult.success_empty(success_reason={"action": "filtered"})
```

<details>
<summary><strong>Advanced: Batch-Aware Transforms</strong></summary>

For aggregation transforms that process multiple rows together, build an output
contract for the aggregate shape in `__init__` and return a `PipelineRow` with
that contract:

```python
from typing import Any

from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract_factory import create_contract_from_config


class BatchStatsTransform(BaseTransform):
    name = "batch_stats"
    determinism = Determinism.DETERMINISTIC
    is_batch_aware = True  # Receives list[PipelineRow] instead of PipelineRow

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._output_schema_config = SchemaConfig(
            mode="fixed",
            fields=(
                FieldDefinition(name="count", field_type="int", required=True),
                FieldDefinition(name="sum", field_type="float", required=True),
                FieldDefinition(name="mean", field_type="float", required=True),
            ),
        )
        self._aggregate_output_contract = create_contract_from_config(self._output_schema_config)

    def process(self, rows: list[PipelineRow], ctx: TransformContext) -> TransformResult:
        if not rows:
            return TransformResult.error({"reason": "invalid_input", "error": "empty batch"})

        values: list[int | float] = []
        for index, row in enumerate(rows):
            value = row["value"]
            # Row data: never coerced, never raised. `type()`, not isinstance:
            # bool is an int. The reason names the row index, field and type,
            # never the value.
            if type(value) not in (int, float):
                return TransformResult.error(
                    {
                        "reason": "invalid_input",
                        "error_type": "wrong_type",
                        "field": "value",
                        "expected": "int or float",
                        "actual_type": type(value).__name__,
                        "error": f"must be int or float, got {type(value).__name__} in row {index}",
                    },
                    retryable=False,
                )
            values.append(value)

        total = float(sum(values))
        return TransformResult.success(
            PipelineRow(
                {"count": len(rows), "sum": total, "mean": total / len(rows)},
                self._aggregate_output_contract,
            ),
            success_reason={"action": "aggregated"},
        )
```

**Pipeline config:**

```yaml
aggregations:
- name: compute_stats
  plugin: batch_stats
  input: processed           # Explicit input connection
  on_success: stats_out      # Named output connection
  on_error: discard          # Sink name for batch errors, or 'discard'
  trigger:
    count: 100  # Process every 100 rows
  output_mode: transform     # Emit the aggregate row the plugin builds (default)
  expected_output_count: 1   # Assert N inputs → 1 output
```

**Declare every column `process()` reads.** A real batch transform validates its
config, calls `self._initialize_declared_input_fields(cfg)`, and folds each
configured input column into `schema.required_fields` on the `SchemaConfig` it
stores as `self._schema_config` (`batch_threshold_summary.py` is the pattern).
Before `process()` runs, the engine checks every buffered row for those fields
(`schema_required_input_fields()`); a row that omits one fails the whole batch
through the aggregation's `on_error`, or fails the collector's group, with a
reason naming the field and the batch row index. A column you read without
declaring it reaches `row[...]` as a `KeyError` that ends the run.
`tests/invariants/test_batch_transforms_read_only_declared_fields.py` holds every
built-in batch transform to this. A column you read only when present
(`if field in row`) is optional and is not declared.

**Reject a wrongly-typed value by returning an error, never by raising.** Under
an `observed` schema (or a field the schema leaves untyped) a value's type is
row data, so a batch transform must check it, as the example does, and must
not coerce it. One bad row fails the WHOLE batch: a sum or a statistic over the
rows that happened to be good would describe a set nobody asked for. The
aggregation's `on_error` then routes every buffered row, with its original
values, to the named sink, or records them as discarded; at a collector the
group fails. When the check sits in a helper that returns a value rather than a
`TransformResult`, raise a plugin-owned exception from the helper and convert it
once in `process()`: the built-in batch transforms raise `BatchRowTypeError`
(`src/elspeth/plugins/transforms/_batch_row_types.py`; `batch_threshold_summary.py`
is the pattern) and return `TransformResult.error(exc.as_reason(), retryable=False)`.
A bare `raise TypeError(...)` is the defect this replaces: nothing in the engine
converts it, so it ends the run with a traceback and no terminal outcomes.
`tests/unit/plugins/test_process_path_type_error_gate.py` fails the build on an
explicit `raise TypeError` that a plugin class's own `process` reaches through
its methods, same-module bases and module functions. It does not follow helper
objects the class composes or code in another module, so a green gate is not
proof that a plugin cannot abort
([CONTRIBUTING](CONTRIBUTING.md#gate-no-bare-typeerror-on-a-plugin-process-path)
lists its blind spots).

</details>

<details>
<summary><strong>Advanced: Deaggregation Transforms</strong></summary>

For transforms that expand one row into multiple rows:

```python
from elspeth.contracts.contract_propagation import propagate_contract


class ExpandItemsTransform(BaseTransform):
    name = "expand_items"
    determinism = Determinism.DETERMINISTIC
    creates_tokens = True  # Engine creates new tokens for each output
    declared_output_fields = frozenset({"item", "item_index"})

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        items = row["items"]
        # A list-shaped field is typed `any` by the source, which validates
        # nothing, so check it here. PipelineRow deep-freezes row data, so a
        # list arrives as a tuple. Never coerce and never raise: iterating a
        # str or a dict would fabricate rows, and a raise aborts the run.
        if type(items) not in (list, tuple):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": "items",
                    "error_type": "wrong_type",
                    "error": f"must be a list, got {type(items).__name__}",
                },
                retryable=False,
            )

        output_rows = []
        for i, item in enumerate(items):
            output = {**row.to_dict(), "item": item, "item_index": i}
            output_contract = propagate_contract(
                row.contract,
                output,
                transform_adds_fields=True,
            )
            output_contract = self._apply_declared_output_field_contracts(output_contract)
            output_contract = self._align_output_contract(output_contract)
            output_rows.append(PipelineRow(output, output_contract))

        return TransformResult.success_multi(output_rows, success_reason={"action": "split"})
```

For production deaggregation code, use `line_explode` as the reference pattern:
it declares output fields, builds `_output_schema_config`, propagates contracts,
and returns homogeneous `PipelineRow` outputs.

**Token semantics:**

- `creates_tokens=True` + `success_multi()` → New tokens per output
- `creates_tokens=False` + `success_multi()` → RuntimeError

</details>

---

## Creating a Source Plugin

Sources load data from external systems. **Sources can coerce input data at the
ingestion boundary.**

```python
from collections.abc import Iterator
from typing import Any
from pydantic import Field, ValidationError

from elspeth.contracts import Determinism, PluginSchema, SourceRow
from elspeth.contracts.contract_builder import ContractBuilder
from elspeth.contracts.schema_contract_factory import create_contract_from_config
from elspeth.plugins.infrastructure.base import BaseSource
from elspeth.plugins.infrastructure.config_base import SourceDataConfig
from elspeth.contracts.contexts import SourceContext
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config


class MySourceConfig(SourceDataConfig):
    """Inherits path, schema, and on_validation_failure."""
    skip_header: bool = Field(default=True, description="Skip the first line of the file.")


class MySource(BaseSource):
    """Load data from a custom format."""

    name = "my_source"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:0000000000000000"  # Replace before CI
    config_model = MySourceConfig

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = MySourceConfig.from_dict(config, plugin_name=self.name)
        self._path = cfg.resolved_path()
        self._skip_header = cfg.skip_header
        self._on_validation_failure = cfg.on_validation_failure

        self._schema_config = cfg.schema_config
        self._initialize_declared_guaranteed_fields(self._schema_config)

        # CRITICAL: allow_coercion=True for sources
        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config, "MySourceSchema", allow_coercion=True
        )
        self.output_schema = self._schema_class

        initial_contract = create_contract_from_config(self._schema_config)
        if initial_contract.locked:
            self.set_schema_contract(initial_contract)
            self._contract_builder: ContractBuilder | None = None
        else:
            self._contract_builder = ContractBuilder(initial_contract)
        self._first_valid_row_processed = False

    def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
        if not self._path.exists():
            raise FileNotFoundError(f"File not found: {self._path}")
        self._first_valid_row_processed = False

        with open(self._path) as f:
            lines = f.readlines()

        if self._skip_header and lines:
            lines = lines[1:]

        for index, line in enumerate(lines):
            row = self._parse_line(line)

            try:
                validated = self._schema_class.model_validate(row)
                validated_row = validated.to_row()

                if self._contract_builder is not None and not self._first_valid_row_processed:
                    field_resolution = {field: field for field in validated_row}
                    self._contract_builder.process_first_row(validated_row, field_resolution)
                    self.set_schema_contract(self._contract_builder.contract)
                    self._first_valid_row_processed = True

                contract = self.require_schema_contract()
                if contract.locked:
                    violations = contract.validate(validated_row)
                    if violations:
                        error_msg = "; ".join(str(v) for v in violations)
                        ctx.record_validation_error(
                            row=validated_row,
                            error=error_msg,
                            schema_mode=self._schema_config.mode,
                            destination=self._on_validation_failure,
                        )
                        if self._on_validation_failure != "discard":
                            yield SourceRow.quarantined(
                                row=validated_row,
                                error=error_msg,
                                destination=self._on_validation_failure,
                                source_row_index=index,
                            )
                        continue

                yield SourceRow.valid(validated_row, contract=contract, source_row_index=index)

            except ValidationError as e:
                ctx.record_validation_error(
                    row=row,
                    error=str(e),
                    schema_mode=self._schema_config.mode or "observed",
                    destination=self._on_validation_failure,
                )
                if self._on_validation_failure != "discard":
                    yield SourceRow.quarantined(
                        row=row,
                        error=str(e),
                        destination=self._on_validation_failure,
                        source_row_index=index,
                    )

    def _parse_line(self, line: str) -> dict[str, Any]:
        parts = line.strip().split(",")
        return {"id": parts[0], "value": parts[1]} if len(parts) >= 2 else {}

    def close(self) -> None:
        pass
```

### SourceRow Options

```python
# Valid row - proceed to processing
SourceRow.valid({"id": 1, "value": 100}, contract=source_contract, source_row_index=0)

# Quarantined - route to on_validation_failure sink
SourceRow.quarantined(
    row=raw_row,
    error="Invalid type",
    destination="quarantine_sink",
    source_row_index=0,
)
```

Valid source rows must carry a `SchemaContract`. Create the contract from the
effective source schema, update it through `ContractBuilder` for observed or
flexible first-row inference, then pass it to `SourceRow.valid(..., contract=...)`.

Both constructors also require a keyword-only `source_row_index`: the
source-authored row position within its emission stream. It has no default, so
omitting it raises `TypeError`.

---

## Creating a Sink Plugin

Built-in sinks publish effects through a recoverable protocol. A new built-in
sink must declare its effect protocol version and implement inspection,
preparation, commit, and reconciliation with the typed contracts in
[`contracts/sink_effects.py`](src/elspeth/contracts/sink_effects.py).
The engine reserves and records effect identity before publication. A sink
then prepares a plan, commits the effect, and can reconcile an uncertain
outcome after interruption. The committed result includes artifact identity,
content hash, and size for audit attribution.

Use [`CSVSink`](src/elspeth/plugins/sinks/csv_sink.py) as a maintained local-file
example, including its staged file and directory-sync behaviour. Shared local
file helpers are in
[`_local_file_effects.py`](src/elspeth/plugins/sinks/_local_file_effects.py).
Remote sinks have different reconciliation requirements; see
[`AWSS3Sink`](src/elspeth/plugins/sinks/aws_s3_sink.py) and
[`_remote_object_effects.py`](src/elspeth/plugins/sinks/_remote_object_effects.py).

`BaseSink` also requires concrete `write()`, `flush()`, and `close()` methods.
For a recoverable sink, `write()` must refuse direct publication; `flush()` and
`close()` can be no-ops when effect commit owns all handles, as in `CSVSink`.
Implementing only the older `write()` / `flush()` pattern does not provide
recovery. Run
[`test_sink_effect_contract.py`](tests/unit/contracts/test_sink_effect_contract.py)
and the relevant sink effect tests before registering one.

---

## Plugin Registration

Built-in plugins are discovered dynamically by scanning configured plugin
directories. For a new top-level built-in plugin, put the file in one of these
directories and make the class inherit the correct base:

| Plugin type | Directory |
|-------------|-----------|
| Source | `src/elspeth/plugins/sources/` |
| Transform | `src/elspeth/plugins/transforms/` |
| Sink | `src/elspeth/plugins/sinks/` |

Discovery is non-recursive. If you add a new subdirectory, update
`PLUGIN_SCAN_CONFIG` in `src/elspeth/plugins/infrastructure/discovery.py`.
`PluginManager.register_builtin_plugins()` turns discovered classes into pluggy
hook implementations at startup, so there is no separate CLI registry to edit.

After adding a plugin, run discovery-focused tests. Some tests assert the exact
built-in plugin count, so a new plugin may require updating those expectations.

---

## Schema Configuration

All data-processing plugins require schema configuration.

### Schema Modes

| Mode | Behavior | Extra Fields |
|------|----------|--------------|
| `observed` | Accept any fields (types inferred from data) | Allowed |
| `fixed` | Only declared fields | Rejected |
| `flexible` | Declared required, extras allowed | Allowed |

### YAML Examples

```yaml
# Accept anything
schema:
  mode: observed

# Fixed - only these fields
schema:
  mode: fixed
  fields:
    - "id: int"
    - "name: str"
    - "active: bool"

# At least these, allow more
schema:
  mode: flexible
  fields:
    - "id: int"
    - "value: float"
```

### Supported Types

`str`, `int`, `float`, `bool`, `any`

---

## Contract Testing

Every plugin **must** pass protocol contract tests.

### Quick Test Setup

```python
# tests/unit/contracts/transform_contracts/test_my_transform_contract.py
import pytest
from elspeth.plugins.transforms.my_transform import MyTransform
from .test_transform_protocol import TransformContractPropertyTestBase


class TestMyTransformContract(TransformContractPropertyTestBase):
    @pytest.fixture
    def transform(self):
        return MyTransform({
            "schema": {"mode": "observed"},
            "multiplier": 2,
        })

    @pytest.fixture
    def valid_input(self):
        return {"id": 1, "value": 10.0}

    # 15+ contract tests are inherited automatically!
```

### Running Tests

```bash
# All contract tests
.venv/bin/python -m pytest tests/unit/contracts/ -v

# Specific plugin
.venv/bin/python -m pytest tests/unit/contracts/transform_contracts/test_my_transform_contract.py -v
```

### Test Base Classes

| Base Class | Location | Inherited Tests |
|------------|----------|-----------------|
| `SourceContractPropertyTestBase` | `tests/unit/contracts/source_contracts/` | 14 tests |
| `TransformContractPropertyTestBase` | `tests/unit/contracts/transform_contracts/` | 15 tests |
| `SinkDeterminismContractTestBase` | `tests/unit/contracts/sink_contracts/` | 17 tests |

<details>
<summary><strong>Contract Tests Reference</strong></summary>

### Source Contracts

| Contract | Test |
|----------|------|
| Has `name` attribute | `test_source_has_name` |
| Has `output_schema` attribute | `test_source_has_output_schema` |
| `load()` returns iterator | `test_load_returns_iterator` |
| `load()` yields `SourceRow` only | `test_load_yields_source_rows` |
| `close()` is idempotent | `test_close_is_idempotent` |

### Transform Contracts

| Contract | Test |
|----------|------|
| Has `name` attribute | `test_transform_has_name` |
| Has `input_schema` attribute | `test_transform_has_input_schema` |
| Has `output_schema` attribute | `test_transform_has_output_schema` |
| `process()` returns `TransformResult` | `test_process_returns_transform_result` |
| `close()` is idempotent | `test_close_is_idempotent` |

### Sink Contracts

The general [sink protocol tests](tests/unit/contracts/sink_contracts/test_sink_protocol.py)
cover class and artifact metadata. The
[effect contract tests](tests/unit/contracts/test_sink_effect_contract.py)
cover the publication boundary. A new sink also needs focused tests for
commit, uncertain outcomes, reconciliation, and any remote-service behaviour.

</details>

---

## Troubleshooting

### "Plugin not found"

```
KeyError: 'my_transform'
```

**Fix:** Confirm the plugin file lives in a scanned directory and the class
inherits the correct base class. For plugins in new subdirectories, add the
subdirectory to `PLUGIN_SCAN_CONFIG` in
`src/elspeth/plugins/infrastructure/discovery.py`.

### "schema is required"

```
ValidationError: schema_config is required
```

**Fix:** Extend `TransformDataConfig`, not `PluginConfig`:

```python
# Wrong
class MyConfig(PluginConfig): ...

# Right
class MyConfig(TransformDataConfig): ...
```

### "has no attribute 'name'"

**Fix:** Make `name` a class attribute, not instance attribute:

```python
class MyTransform(BaseTransform):
    name = "my_transform"  # Class attribute (correct)

    def __init__(self, config):
        self.name = "my_transform"  # Instance attribute (wrong)
```

### Validation failures in transform

**Fix:** Check `allow_coercion` setting:

```python
# Source: allow_coercion=True (external boundary)
# Transform/Sink: allow_coercion=False (trust upstream)
# Transform external response: validate/coerce immediately at that call boundary
```

---

## Checklist for New Plugins

- [ ] Has `name` class attribute
- [ ] Has `determinism` declared in the plugin's own class body (no default; undeclared raises `TypeError`)
- [ ] Has `plugin_version`, `source_file_hash`, and `config_model` class attributes
- [ ] Has catalogue reference content (`usage_when_to_use`, `usage_when_not_to_use`, `example_use`, `capability_tags`)
- [ ] Has required schema attributes (`input_schema`, `output_schema`)
- [ ] Config extends the correct data config base (`TransformDataConfig`, `SourceDataConfig`, `SinkPathConfig`, or a narrower sink/source config)
- [ ] Every config field declares a pydantic `description`
- [ ] Schema created with correct `allow_coercion`
- [ ] Source valid rows call `SourceRow.valid(row, contract=contract, source_row_index=index)`
- [ ] Transform successes return `PipelineRow` values, never raw dicts
- [ ] Sink implements the typed effect protocol, including inspect, prepare,
      commit, and reconcile, with tests for interrupted publication
- [ ] Transform field declarations are set (`declared_input_fields`, `declared_output_fields`, `_output_schema_config`) when the plugin requires or adds fields
- [ ] Sink required fields are set with `declared_required_fields`
- [ ] Plugin file lives in a scanned discovery directory, or `PLUGIN_SCAN_CONFIG` was updated
- [ ] `elspeth-lints check --rules plugin_contract.plugin_hashes,plugin_contract.options_metadata,plugin_contract.component_type --root src/elspeth` passes
- [ ] Contract tests pass
- [ ] `close()` is idempotent

---

## See Also

- [Data Trust and Error Handling](docs/guides/data-trust-and-error-handling.md) - External boundaries, quarantine, and plugin error handling
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture and plugin integration points
- [Configuration Reference](docs/reference/configuration.md) - Full configuration options
