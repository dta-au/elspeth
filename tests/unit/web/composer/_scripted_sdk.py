"""Neutral scripted physical-SDK response shapes and live candidate fixtures.

Responses cross the production parser; these helpers do not construct planner
results, bypass provider admission, or depend on a retired planner route.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from litellm import ModelResponse

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_TEST_SESSION_ID = "11111111-1111-4111-8111-111111111111"


@dataclass
class _Function:
    name: str
    arguments: object


@dataclass
class _ToolCall:
    id: str
    function: _Function | None


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall] | None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Mapping[str, object]
    model: str | None = "provider/planner-v1"
    id: str = "request-1"


class _ScriptedCompletion:
    def __init__(self, *responses: _Response | ModelResponse | BaseException) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _Response | ModelResponse:
        self.requests.append(deepcopy(kwargs))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _planner_usage(*, cost: object = 0.01) -> dict[str, object]:
    return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": cost}


def _response(*calls: tuple[str, object], cost: object = 0.01) -> _Response:
    tool_calls = [
        _ToolCall(id=f"call-{index}", function=_Function(name=name, arguments=json.dumps(arguments)))
        for index, (name, arguments) in enumerate(calls, start=1)
    ]
    return _Response(
        choices=[_Choice(message=_Message(content=None, tool_calls=tool_calls))],
        usage=_planner_usage(cost=cost),
    )


def _response_with_call_id(call_id: str, name: str, arguments: object, *, cost: object = 0.01) -> _Response:
    return _Response(
        choices=[
            _Choice(
                message=_Message(
                    content=None,
                    tool_calls=[_ToolCall(id=call_id, function=_Function(name=name, arguments=json.dumps(arguments)))],
                )
            )
        ],
        usage=_planner_usage(cost=cost),
    )


def _response_with_usage(
    *calls: tuple[str, object],
    cost: object = 0.01,
    completion_tokens: object = 5,
) -> _Response:
    response = _response(*calls, cost=cost)
    response.usage = {
        "prompt_tokens": 10,
        "completion_tokens": completion_tokens,
        "total_tokens": 10 + completion_tokens if isinstance(completion_tokens, int) else None,
        "cost": cost,
    }
    return response


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _pipeline(data_dir: Path, *, session_id: str = _TEST_SESSION_ID) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "path": str(data_dir / "blobs" / session_id / "input.csv"),
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {
                    "path": "outputs/result.jsonl",
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
    }


def _budget(**overrides: Any) -> PlannerBudgetPolicy:
    values: dict[str, Any] = {
        "max_total_provider_calls": 4,
        "max_request_bytes": 1_000_000,
        "max_completion_tokens": 800,
        "max_cumulative_provider_cost": Decimal("1.00"),
    }
    values.update(overrides)
    return PlannerBudgetPolicy(**values)


def _text_response(text: str, *, cost: object = 0.01) -> _Response:
    return _Response(
        choices=[_Choice(message=_Message(content=text, tool_calls=None))],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": cost},
    )


def _candidate_context(tmp_path: Path, session_id: str) -> tuple[ToolContext, PolicyCatalogView, Path]:
    """Production-shaped candidate context: data_dir, session_id, real catalog."""
    blobs = tmp_path / "blobs" / session_id
    blobs.mkdir(parents=True, exist_ok=True)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    context = ToolContext(
        catalog=policy_catalog,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_id=session_id,
    )
    return context, policy_catalog, blobs


def _bad_source(blobs: Path, **overrides: Any) -> dict[str, Any]:
    """A csv source whose options fail prevalidation (no ``schema``)."""
    return {
        "plugin": "csv",
        "on_success": "clean",
        "options": {"path": str(blobs / "in.csv"), **overrides},
        "on_validation_failure": "discard",
    }


_INJECTED_PLUGIN = "no_such_plugin_xyz"


_INJECTED_CLAUSE = f"Invalid options for transform '{_INJECTED_PLUGIN}'"


@dataclass
class _ChoiceWithFinishReason:
    message: _Message
    finish_reason: str | None = None


_PARTIAL_PROPOSAL_ARGUMENTS = '{"pipeline": {"source": {"plugin": "csv", "opti'


def _cut_off_response(*, completion_tokens: int, finish_reason: str | None) -> _Response:
    call = _ToolCall(id="call-1", function=_Function(name="set_pipeline", arguments=_PARTIAL_PROPOSAL_ARGUMENTS))
    usage = dict(_planner_usage())
    usage["completion_tokens"] = completion_tokens
    usage["total_tokens"] = 10 + completion_tokens
    choice = cast(Any, _ChoiceWithFinishReason(message=_Message(content=None, tool_calls=[call]), finish_reason=finish_reason))
    return _Response(choices=[choice], usage=usage)


def _length_stopped_tool_response(tmp_path: Path, *, discovery: bool = False) -> _Response:
    response = _response(("list_sources", {}) if discovery else ("set_pipeline", {"pipeline": _pipeline(tmp_path)}))
    choice = _ChoiceWithFinishReason(message=response.choices[0].message, finish_reason="length")
    response.choices[0] = cast(Any, choice)
    return response
