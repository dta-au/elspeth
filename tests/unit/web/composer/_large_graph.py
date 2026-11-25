"""A synthetic composition state shaped like a large real pipeline.

Built from shape numbers only: the mix of node kinds (11 LLM, 8
``value_transform``, 2 ``field_mapper``, 2 gates, 1 aggregation, 1
``json_explode``), one source and three sinks, about 200 KB of state and
more than half of the credential scanner's default node budget. Every string
is generated filler; nothing here is copied from a stored session.
"""

from __future__ import annotations

from typing import Any

from elspeth.contracts.credential_material import CredentialTraversalPolicy, find_credential_material
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.state import CompositionState

_FILLER = "Summarise the record for the reviewer in plain words and keep every figure exactly as written. "

# One entry per node of the 25-node shape: (kind, entries in its longest list, filler repeats in its prompt).
_SHAPE: tuple[tuple[str, int, int], ...] = (
    ("llm", 150, 150),
    ("llm", 145, 150),
    ("llm", 145, 150),
    ("llm", 120, 150),
    ("llm", 105, 150),
    ("llm", 105, 150),
    ("llm", 105, 150),
    ("llm", 95, 150),
    ("llm", 70, 150),
    ("llm", 55, 150),
    ("llm", 40, 150),
    ("value_transform", 40, 12),
    ("value_transform", 40, 12),
    ("value_transform", 35, 12),
    ("value_transform", 30, 12),
    ("value_transform", 25, 12),
    ("value_transform", 12, 12),
    ("value_transform", 10, 12),
    ("value_transform", 8, 12),
    ("field_mapper", 12, 0),
    ("field_mapper", 10, 0),
    ("gate", 0, 0),
    ("gate", 0, 0),
    ("aggregation", 20, 0),
    ("json_explode", 4, 0),
)

LARGE_GRAPH_NODE_COUNT = len(_SHAPE)


def _node(index: int, kind: str, entries: int, repeats: int) -> dict[str, Any]:
    node_id = f"step_{index:03d}"
    base: dict[str, Any] = {
        "id": node_id,
        "input": f"conn_{index:03d}",
        "on_success": f"conn_{index + 1:03d}",
        "on_error": "discard",
        "description": f"Step {index} of the synthetic pipeline.",
    }
    fields = [f"field_{index:03d}_{position:03d}" for position in range(entries)]
    if kind == "llm":
        return base | {
            "node_type": "transform",
            "plugin": "llm",
            "options": {
                "profile": "default",
                "schema": {"mode": "observed"},
                "response_field": f"summary_{index:03d}",
                "response_format": "text",
                "required_input_fields": fields,
                "output_fields": [{"name": f"summary_{index:03d}", "type": "str"}],
                "system_prompt": _FILLER * 4,
                "prompt_template": f"Step {index}. " + _FILLER * repeats,
            },
        }
    if kind == "value_transform":
        return base | {
            "node_type": "transform",
            "plugin": "value_transform",
            "options": {
                "schema": {"mode": "observed"},
                "operations": [{"target": name, "expression": f"row['{name}']"} for name in fields],
                "notes": _FILLER * repeats,
            },
        }
    if kind == "field_mapper":
        return base | {
            "node_type": "transform",
            "plugin": "field_mapper",
            "options": {"schema": {"mode": "observed"}, "mapping": {name: f"renamed_{name}" for name in fields}},
        }
    if kind == "gate":
        return base | {
            "node_type": "gate",
            "plugin": None,
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": f"row['field_{index:03d}_000'] is not None",
            "routes": {"true": f"conn_{index + 1:03d}", "false": "review"},
        }
    if kind == "aggregation":
        return base | {
            "node_type": "aggregation",
            "plugin": "batch_duplicate_annotator",
            "options": {"schema": {"mode": "observed"}, "key_fields": fields},
            "trigger": {"count": 50},
            "output_mode": "passthrough",
        }
    return base | {
        "node_type": "transform",
        "plugin": "json_explode",
        "options": {"schema": {"mode": "observed"}, "array_field": "items", "output_fields": fields},
    }


def large_graph_dict(node_count: int = LARGE_GRAPH_NODE_COUNT) -> dict[str, Any]:
    """The state as a plain dict; nodes beyond the 25-node shape repeat it in order."""
    nodes = [_node(index, *_SHAPE[index % len(_SHAPE)]) for index in range(node_count)]
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "conn_000",
                # A declared schema: an observed one would make the 1,600 generated field names a
                # source data-contract demand, which is a different limit from the one under test.
                "options": {"path": "inputs/records.csv", "schema": {"mode": "fixed", "fields": ["record_id: str"]}},
                "on_validation_failure": "discard",
            }
        },
        "nodes": nodes,
        "edges": [],
        "outputs": [
            {"name": f"conn_{node_count:03d}", "plugin": "csv", "options": {"path": "outputs/main.csv"}, "on_write_failure": "discard"},
            {"name": "review", "plugin": "csv", "options": {"path": "outputs/review.csv"}, "on_write_failure": "discard"},
            {"name": "rejected", "plugin": "json", "options": {"path": "outputs/rejected.json"}, "on_write_failure": "discard"},
        ],
        "metadata": {"name": "Synthetic large pipeline", "description": "Generated for size tests."},
        "version": 1,
    }


def large_graph_state(node_count: int = LARGE_GRAPH_NODE_COUNT) -> CompositionState:
    return CompositionState.from_dict(large_graph_dict(node_count))


def state_bytes(state: CompositionState) -> int:
    return len(canonical_json(state.to_dict()).encode("utf-8"))


def default_scan_finding(state: CompositionState) -> str | None:
    """What the scanner's DEFAULT bounds say about the whole state, as a category or ``None``."""
    finding = find_credential_material(state.to_dict(), CredentialTraversalPolicy(), purpose="admission")
    return None if finding is None else finding.category
