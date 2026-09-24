"""The replayed planner transcript keeps the model's key order (Defect A).

Plan: ``docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md`` T2.

When the compose loop replaces a ``set_pipeline`` call's arguments in the
provider transcript (custody projection, required-control finalization, inline
custody), ``_replace_llm_tool_call_arguments`` re-serialises them. Map order is
semantic in three places the planner authors: ``sources`` (ingest order and the
"first source"), ``row_union.branches`` (release order) and mapping-form
``coalesce.branches`` (the ``last_wins`` collision order). A sorted re-serialisation
shows the model an alphabetised pipeline on its next turn, so the model reasons
about an order it never authored. These tests pin the model's order on every
rewrite path the compose loop takes:

- the replay function itself, on every dialect (exact bytes);
- the success path (``tool_batch.py`` custody projection, every set_pipeline);
- the required-control finalization path, auto-commit and explicit approval;
- the inline-custody path (``inline_blob`` rewritten to ``blob_id``).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

import elspeth.web.composer.tool_batch as tool_batch
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tool_batch import _replace_llm_tool_call_arguments
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from tests.integration.web.composer.test_freeform_proposal_prevalidation import (
    _harness,
    _inline_pipeline_args,
    _ScriptedLLM,
    _tool_turn,
)
from tests.integration.web.composer.test_freeform_required_controls import (
    _required_textract_policy,
    _textract_llm_mapper_args,
)
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM
from tests.unit.web.composer.test_planner_authoring_aids import _empty_state

_MODEL_ORDER = ["zeta", "alpha"]
_SOURCE_ORDER = ["zeta_src", "alpha_src"]
_NODE_KEY_ORDER = ["id", "node_type", "input", "on_success", "branches"]


def _csv_source(on_success: str) -> dict[str, Any]:
    return {
        "plugin": "csv",
        "on_success": on_success,
        "options": {"path": f"{on_success}.csv", "schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }


def _reversed_order_pipeline() -> dict[str, Any]:
    """A set_pipeline whose three order-semantic maps all run ``zeta`` before ``alpha``."""
    return {
        "sources": {
            "zeta_src": _csv_source("zeta_rows"),
            "alpha_src": _csv_source("alpha_rows"),
        },
        "nodes": [
            {
                "id": "union",
                "node_type": "row_union",
                "input": "zeta_rows",
                "on_success": "unioned",
                "branches": {"zeta": "zeta_rows", "alpha": "alpha_rows"},
            },
            {
                "id": "split",
                "node_type": "gate",
                "input": "unioned",
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["zeta", "alpha"],
            },
            {
                "id": "merge",
                "node_type": "coalesce",
                "input": "zeta",
                "on_success": "out",
                "branches": {"zeta": "zeta", "alpha": "alpha"},
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {"path": "out.json", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    }


def _node(pipeline: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(node for node in pipeline["nodes"] if node["id"] == node_id)


def _assert_model_order(pipeline: dict[str, Any]) -> None:
    assert list(pipeline["sources"]) == _SOURCE_ORDER
    assert list(_node(pipeline, "union")["branches"]) == _MODEL_ORDER
    assert list(_node(pipeline, "merge")["branches"]) == _MODEL_ORDER


def _replayed_pipeline(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    """Decode the set_pipeline arguments a provider turn received for ``call_id``."""
    for message in messages:
        if message["role"] != "assistant" or "tool_calls" not in message or not message["tool_calls"]:
            continue
        for call in message["tool_calls"]:
            if call["id"] == call_id:
                decoded = json.loads(call["function"]["arguments"])
                assert list(decoded) == ["pipeline"], decoded
                pipeline = decoded["pipeline"]
                assert type(pipeline) is dict
                return pipeline
    raise AssertionError(f"tool call {call_id!r} is not in the provider transcript")


class TestReplayFunctionKeepsKeyOrder:
    """Unit: ``_replace_llm_tool_call_arguments`` on every dialect."""

    @pytest.mark.parametrize("dialect", list(ToolContractDialect), ids=[member.value for member in ToolContractDialect])
    def test_set_pipeline_replay_keeps_map_and_node_key_order(self, dialect: ToolContractDialect) -> None:
        pipeline = _reversed_order_pipeline()
        _assert_model_order(pipeline)
        assert list(_node(pipeline, "merge")) == _NODE_KEY_ORDER
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "set_pipeline", "arguments": "{}"}}],
            }
        ]

        _replace_llm_tool_call_arguments(messages, tool_call_id="call_1", arguments=pipeline, dialect=dialect, semantic=True)

        encoded = messages[0]["tool_calls"][0]["function"]["arguments"]
        replayed = _replayed_pipeline(messages, "call_1")
        _assert_model_order(replayed)
        assert list(_node(replayed, "merge")) == _NODE_KEY_ORDER
        assert list(_node(replayed, "union")) == _NODE_KEY_ORDER
        # Exact bytes: compact separators, authored order. The sorted form has the
        # same decoded content, so only a byte pin tells the two apart.
        expected_provider_arguments = encode_semantic_arguments("set_pipeline", dialect, pipeline)
        assert encoded == json.dumps(expected_provider_arguments, separators=(",", ":"))
        assert encoded != json.dumps(expected_provider_arguments, sort_keys=True, separators=(",", ":"))


class _RecordingLLM(_FakeComposeLLM):
    """Stores a deep copy of every provider turn's messages.

    ``_replace_llm_tool_call_arguments`` mutates ``function["arguments"]`` in
    place, so a shallow copy would show only the last rewrite.
    """

    def __init__(self, responses: Any) -> None:
        super().__init__(responses)
        self.seen: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: Any, tools: Any) -> Any:
        self.seen.append(copy.deepcopy(list(messages)))
        return await super().__call__(messages, tools)


def _record_replay_callers(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the ``tool_batch`` line each transcript rewrite is called from.

    The compose loop reaches the replay function from several callers; each
    compose-loop test asserts which ones its fixture reached, so a fixture that
    silently stops reaching a path fails instead of passing vacuously.
    """
    caller_lines: list[int] = []
    real_replace = tool_batch._replace_llm_tool_call_arguments

    def _recording_replace(*args: Any, **kwargs: Any) -> None:
        caller_lines.append(sys._getframe(1).f_lineno)
        real_replace(*args, **kwargs)

    monkeypatch.setattr(tool_batch, "_replace_llm_tool_call_arguments", _recording_replace)
    return caller_lines


@pytest.mark.asyncio
async def test_success_path_replay_keeps_model_order_on_next_provider_turn(
    fake_composer_service: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every set_pipeline is re-serialised through the custody projection before dispatch."""
    pipeline = _reversed_order_pipeline()
    caller_lines = _record_replay_callers(monkeypatch)
    llm = _RecordingLLM(
        (
            _fake_llm_response(tool_calls=({"id": "call_sp", "name": "set_pipeline", "arguments": pipeline},)),
            _fake_llm_response(content="Done."),
        )
    )

    await fake_composer_service._run_one_turn_for_test(llm=llm, session_id=result_session_id)

    assert len(llm.seen) >= 2, "the fixture must reach a second provider turn"
    assert len(caller_lines) == 1, caller_lines
    replayed = _replayed_pipeline(llm.seen[1], "call_sp")
    _assert_model_order(replayed)
    assert list(_node(replayed, "merge")) == _NODE_KEY_ORDER


def _with_reversed_coalesce(args: dict[str, Any], *, sink: str) -> dict[str, Any]:
    """Append a fork gate and a mapping-form coalesce whose branches run zeta, alpha."""
    args["nodes"][-1]["on_success"] = "mapped_rows"
    args["nodes"].append(
        {
            "id": "split",
            "node_type": "gate",
            "input": "mapped_rows",
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ["zeta", "alpha"],
        }
    )
    args["nodes"].append(
        {
            "id": "merge",
            "node_type": "coalesce",
            "input": "zeta",
            "on_success": sink,
            "branches": {"zeta": "zeta", "alpha": "alpha"},
        }
    )
    return args


def _node_plugins(pipeline: dict[str, Any]) -> list[object]:
    return [node["plugin"] if "plugin" in node else None for node in pipeline["nodes"]]


_WIRED_PLUGINS = [
    "aws_textract_document_analysis",
    "aws_bedrock_prompt_shield",
    "llm",
    "aws_bedrock_content_safety",
    "field_mapper",
    None,
    None,
]


@pytest.mark.asyncio
async def test_auto_commit_finalization_replay_keeps_coalesce_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Required-control finalization rewrites the transcript (auto-commit caller).

    Only the transcript order is asserted: this fixture's reply also carries a
    runtime-preflight note, which is not what the test is about.
    """
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    llm = _ScriptedLLM(
        _tool_turn("call_auto", "set_pipeline", _with_reversed_coalesce(_textract_llm_mapper_args(tmp_path), sink="output_rows"))
    )
    caller_lines = _record_replay_callers(monkeypatch)

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        await harness.service.compose(
            "Build and apply a Textract to LLM pipeline.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    # Success-path rewrite, then the finalization rewrite (the controls were wired).
    assert len(caller_lines) == 2, caller_lines
    replayed = _replayed_pipeline(llm.message_snapshots[1], "call_auto")
    assert _node_plugins(replayed) == _WIRED_PLUGINS, "the transcript must be the finalized (auto-wired) arguments"
    assert list(_node(replayed, "merge")["branches"]) == _MODEL_ORDER


@pytest.mark.asyncio
async def test_explicit_finalization_and_custody_replay_keeps_coalesce_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit approval: success path, finalization rewrite, then the inline-custody rewrite."""
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_explicit", "set_pipeline", _with_reversed_coalesce(_textract_llm_mapper_args(tmp_path), sink="output_rows"))
    )
    caller_lines = _record_replay_callers(monkeypatch)

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        await harness.service.compose(
            "Build a Textract to LLM pipeline and prepare it for review.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert len(caller_lines) == 3, caller_lines
    replayed = _replayed_pipeline(llm.message_snapshots[1], "call_explicit")
    assert _node_plugins(replayed) == _WIRED_PLUGINS
    assert "blob_id" in replayed["source"] and "inline_blob" not in replayed["source"]
    assert list(_node(replayed, "merge")["branches"]) == _MODEL_ORDER


@pytest.mark.asyncio
async def test_inline_custody_replay_keeps_coalesce_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline custody alone (no required controls): ``inline_blob`` becomes ``blob_id`` in the transcript."""
    harness = _harness(tmp_path)
    args = _inline_pipeline_args(tmp_path)
    args["source"]["on_success"] = "mapped_rows"
    args["nodes"] = [
        {
            "id": "split",
            "node_type": "gate",
            "input": "mapped_rows",
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ["zeta", "alpha"],
        },
        {
            "id": "merge",
            "node_type": "coalesce",
            "input": "zeta",
            "on_success": "main",
            "branches": {"zeta": "zeta", "alpha": "alpha"},
        },
    ]
    llm = _ScriptedLLM(_tool_turn("call_inline", "set_pipeline", args))
    caller_lines = _record_replay_callers(monkeypatch)

    with patch.object(harness.service, "_call_llm", new=llm):
        await harness.service.compose(
            "Prepare this pipeline for review.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    # Success-path rewrite, then the inline-custody rewrite; no finalization change.
    assert len(caller_lines) == 2, caller_lines
    replayed = _replayed_pipeline(llm.message_snapshots[1], "call_inline")
    assert "blob_id" in replayed["source"] and "inline_blob" not in replayed["source"]
    assert list(_node(replayed, "merge")["branches"]) == _MODEL_ORDER
