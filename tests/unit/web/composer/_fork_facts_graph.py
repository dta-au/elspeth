"""Synthetic graphs for the duplicate-consumer facts tests. No user data.

A linear chain of transforms whose first ``shared`` steps all read the
source's connection, so validation reports one ``duplicate_connection_consumer``
naming every one of them. The LLM steps carry long prompts so the cost of
restating a step is visible in bytes. Adapted from the s18 scale review's
generator (``chain`` with ``shared=k``); tests cannot import from ``.claude/``.
"""

from __future__ import annotations

from typing import Any

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec

SHARED = "shared"
SINK = "final_out"


def step_id(index: int, *, llm_count: int) -> str:
    return f"llm_{index:02d}" if index < llm_count else f"map_{index:02d}"


def _options(index: int, *, llm_count: int, prompt_rows: int) -> dict[str, Any]:
    if index < llm_count:
        line = f"Criterion {index}: read the record, weigh the stated evidence against the rubric, and give one grounded finding. "
        return {
            "provider": "openrouter",
            "model": "openai/gpt-4o",
            "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
            "prompt_template": "".join(f"[{row:03d}] {line}" for row in range(prompt_rows)),
            "schema": {"mode": "observed"},
        }
    return {
        "mapping": {f"finding_{index}_{row:02d}": f"report_{index}_{row:02d}" for row in range(30)},
        "schema": {"mode": "observed"},
    }


def chain(*, steps: int, shared: int, llm_count: int, prompt_rows: int = 150) -> CompositionState:
    """``steps`` transforms in a chain; the first ``max(shared, 1)`` read the source connection."""
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success=SHARED,
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
        )
    )
    for index in range(steps):
        state = state.with_node(
            NodeSpec(
                id=step_id(index, llm_count=llm_count),
                node_type="transform",
                plugin="llm" if index < llm_count else "field_mapper",
                input=SHARED if index == 0 or index < shared else f"c{index}",
                on_success=f"c{index + 1}" if index < steps - 1 else SINK,
                on_error="discard",
                options=_options(index, llm_count=llm_count, prompt_rows=prompt_rows),
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
    return state.with_output(OutputSpec(name=SINK, plugin="csv", options={"path": "outputs/final.csv"}, on_write_failure="discard"))
