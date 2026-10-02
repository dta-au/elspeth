"""Which recorded tool failure names the empty-state blocker (finding #29).

``blocking_result_from_tool_invocations`` promises "the most recent failed
build/edit tool result". Only its succeeded-without-mutating arm skipped
discovery tools, so a discovery call that failed AFTER a failed build tool
(an ARG_ERROR, a PLUGIN_CRASH, or a ``success=false`` payload) became the
user-facing ``Cause:``, the ``state_exists`` check detail and the readiness
blocker, and the build failure that actually left the pipeline empty was named
on none of them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.web.composer.no_tool_policy import (
    blocking_result_from_tool_invocations,
    compose_empty_state_message,
    no_mutation_empty_state_validation,
)
from elspeth.web.composer.tools import is_discovery_tool

_DISCOVERY_TOOL = "get_plugin_schema"
_BUILD_TOOL = "set_pipeline"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _invocation(
    tool_name: str,
    status: ComposerToolStatus,
    *,
    error_message: str | None = None,
    error_class: str | None = None,
    result: dict[str, object] | None = None,
) -> ComposerToolInvocation:
    arguments = "{}"
    result_canonical = json.dumps(result) if result is not None else None
    now = datetime.now(UTC)
    return ComposerToolInvocation(
        tool_call_id=f"call-{tool_name}",
        tool_name=tool_name,
        arguments_canonical=arguments,
        arguments_hash=_sha(arguments),
        result_canonical=result_canonical,
        result_hash=_sha(result_canonical) if result_canonical is not None else None,
        status=status,
        error_class=error_class,
        error_message=error_message,
        version_before=1,
        version_after=1 if status is ComposerToolStatus.SUCCESS else None,
        started_at=now,
        finished_at=now,
        latency_ms=0,
        actor="test",
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION if status is ComposerToolStatus.ARG_ERROR else None,
    )


_FAILED_BUILD = _invocation(
    _BUILD_TOOL,
    ComposerToolStatus.ARG_ERROR,
    error_class="ToolArgumentError",
    error_message="'source' must be an object, got str",
)
_EXPECTED_BUILD_BLOCKER = "set_pipeline failed before mutation (ToolArgumentError: 'source' must be an object, got str)."

_TRAILING_DISCOVERY_FAILURES = [
    pytest.param(
        _invocation(
            _DISCOVERY_TOOL,
            ComposerToolStatus.ARG_ERROR,
            error_class="ToolArgumentError",
            error_message="'name' must be a known plugin name",
        ),
        id="discovery_arg_error",
    ),
    pytest.param(
        _invocation(
            _DISCOVERY_TOOL,
            ComposerToolStatus.PLUGIN_CRASH,
            error_class="RuntimeError",
            error_message="catalog lookup crashed",
        ),
        id="discovery_plugin_crash",
    ),
    pytest.param(
        _invocation(
            _DISCOVERY_TOOL,
            ComposerToolStatus.SUCCESS,
            result={"success": False, "error": "unknown plugin"},
        ),
        id="discovery_success_false",
    ),
]


def test_fixture_tool_classification_is_what_the_cases_assume() -> None:
    """Instrument control: the cases below mean nothing if the names are misclassified."""
    assert is_discovery_tool(_DISCOVERY_TOOL) is True
    assert is_discovery_tool(_BUILD_TOOL) is False


def test_trailing_successful_discovery_call_names_the_build_failure() -> None:
    """Control: the arm that already skipped discovery tools."""
    trailing_ok = _invocation(_DISCOVERY_TOOL, ComposerToolStatus.SUCCESS, result={"success": True, "data": {}})

    assert blocking_result_from_tool_invocations((_FAILED_BUILD, trailing_ok)) == _EXPECTED_BUILD_BLOCKER


@pytest.mark.parametrize("trailing_discovery_failure", _TRAILING_DISCOVERY_FAILURES)
def test_trailing_discovery_failure_does_not_mask_the_build_failure(trailing_discovery_failure: ComposerToolInvocation) -> None:
    blocker = blocking_result_from_tool_invocations((_FAILED_BUILD, trailing_discovery_failure))

    assert blocker == _EXPECTED_BUILD_BLOCKER


@pytest.mark.parametrize("trailing_discovery_failure", _TRAILING_DISCOVERY_FAILURES)
def test_every_user_facing_surface_names_the_build_failure(trailing_discovery_failure: ComposerToolInvocation) -> None:
    """The blocker feeds the chat ``Cause:``, the check detail and the readiness blocker."""
    blocker = blocking_result_from_tool_invocations((_FAILED_BUILD, trailing_discovery_failure))
    validation = no_mutation_empty_state_validation(blocker)
    message = compose_empty_state_message("", blocker=blocker)

    surfaces = [message, *(check.detail for check in validation.checks), *(b.detail for b in validation.readiness.blockers)]
    assert any(_BUILD_TOOL in surface for surface in surfaces)
    for surface in surfaces:
        assert _DISCOVERY_TOOL not in surface


@pytest.mark.parametrize("discovery_failure", _TRAILING_DISCOVERY_FAILURES)
def test_only_discovery_failures_fall_back_to_the_no_build_tool_blocker(discovery_failure: ComposerToolInvocation) -> None:
    """With no build/edit tool at all, the docstring's fallback is the true statement."""
    assert blocking_result_from_tool_invocations((discovery_failure,)) == "the model ended the turn without calling any build/edit tool."


def test_most_recent_build_failure_still_wins_over_an_older_one() -> None:
    older = _invocation(
        "upsert_node",
        ComposerToolStatus.PLUGIN_CRASH,
        error_class="RuntimeError",
        error_message="older crash",
    )

    assert blocking_result_from_tool_invocations((older, _FAILED_BUILD)) == _EXPECTED_BUILD_BLOCKER
