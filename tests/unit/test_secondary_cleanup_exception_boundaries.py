"""Ordinary cleanup failures preserve primary events; control groups stay intact."""

from __future__ import annotations

import asyncio
from typing import NoReturn
from unittest.mock import Mock, patch

import pytest
from botocore.client import BaseClient

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.engine._best_effort import best_effort
from elspeth.web._aws_ecs_acceptance.contracts import AcceptanceCheckError
from elspeth.web._aws_ecs_acceptance.orphan_sweep import _build_orphan_sweep_clients
from elspeth.web.readiness import _cancel_wrapped_on_loop


class _NoSplitControlGroup(BaseExceptionGroup):
    def split(self, *args: object, **kwargs: object) -> NoReturn:
        pytest.fail("secondary cleanup must not invoke a custom group split")


def _control_group() -> _NoSplitControlGroup:
    return _NoSplitControlGroup("control group", [KeyboardInterrupt(), ValueError("secondary failure")])


@pytest.mark.parametrize("grouped", [False, True])
def test_logger_ordinary_failure_preserves_the_inflight_primary_identity(grouped: bool) -> None:
    primary = LookupError("primary event")
    logger_failure = ExceptionGroup("logger failures", [ValueError("secondary")]) if grouped else ValueError("secondary")
    logger = Mock(spec=["warning"])
    logger.warning.side_effect = logger_failure
    with pytest.raises(LookupError) as raised:
        try:
            raise primary
        except LookupError:
            with patch("elspeth.engine._best_effort._slog", new=logger), best_effort("secondary ceremony"):
                raise RuntimeError("ceremony failed") from primary
            raise
    assert raised.value is primary
    logger.warning.assert_called_once()
    assert logger.warning.call_args.kwargs["error_type"] == "RuntimeError"


def test_logger_process_control_group_propagates_intact_without_custom_split() -> None:
    failure = _control_group()
    logger = Mock(spec=["warning"])
    logger.warning.side_effect = failure
    with (
        patch("elspeth.engine._best_effort._slog", new=logger),
        pytest.raises(_NoSplitControlGroup) as raised,
        best_effort("secondary ceremony"),
    ):
        raise RuntimeError("ceremony failed")
    assert raised.value is failure
    assert raised.value.exceptions == failure.exceptions
    logger.warning.assert_called_once()


def test_tier_one_ceremony_failure_never_enters_the_secondary_logger() -> None:
    failure = AuditIntegrityError("retained audit failure")
    logger = Mock(spec=["warning"])
    with (
        patch("elspeth.engine._best_effort._slog", new=logger),
        pytest.raises(AuditIntegrityError) as raised,
        best_effort("secondary ceremony"),
    ):
        raise failure
    assert raised.value is failure
    logger.warning.assert_not_called()


@pytest.mark.parametrize("grouped", [False, True])
def test_partial_client_setup_closes_in_reverse_and_preserves_primary_projection(grouped: bool) -> None:
    primary = RuntimeError("SDK setup failure")
    cleanup_failure = ExceptionGroup("close failures", [ValueError("secondary")]) if grouped else ValueError("secondary")
    first = Mock(spec=BaseClient)
    second = Mock(spec=BaseClient)
    closed: list[str] = []

    def close_first() -> None:
        closed.append("first")

    def close_second() -> NoReturn:
        closed.append("second")
        raise cleanup_failure

    first.close.side_effect = close_first
    second.close.side_effect = close_second
    with (
        patch("boto3.client", autospec=True, side_effect=[first, second, primary]) as create,
        pytest.raises(AcceptanceCheckError) as raised,
    ):
        _build_orphan_sweep_clients("ap-southeast-2")
    assert [call.args[0] for call in create.call_args_list] == ["resourcegroupstaggingapi", "ecs", "elbv2"]
    assert all(call.kwargs["region_name"] == "ap-southeast-2" for call in create.call_args_list)
    assert closed == ["second", "first"]
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()
    assert raised.value.check == "orphan_sweep_runtime"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert raised.value.__context__ is primary


def test_partial_client_setup_process_control_group_propagates_intact_without_split() -> None:
    primary = RuntimeError("SDK setup failure")
    failure = _control_group()
    first = Mock(spec=BaseClient)
    second = Mock(spec=BaseClient)
    second.close.side_effect = failure
    with (
        patch("boto3.client", autospec=True, side_effect=[first, second, primary]) as create,
        pytest.raises(_NoSplitControlGroup) as raised,
    ):
        _build_orphan_sweep_clients("ap-southeast-2")
    assert raised.value is failure
    assert raised.value.__context__ is primary
    assert create.call_count == 3
    second.close.assert_called_once_with()
    first.close.assert_not_called()


def test_loop_cancel_still_schedules_and_drains_the_actual_wrapper() -> None:
    loop = asyncio.new_event_loop()
    wrapped = loop.create_future()
    try:
        _cancel_wrapped_on_loop(wrapped, loop)
        loop.run_until_complete(asyncio.sleep(0))
        assert wrapped.cancelled()
    finally:
        wrapped.cancel()
        loop.close()


def test_loop_cancel_already_closed_does_not_schedule() -> None:
    loop = asyncio.new_event_loop()
    wrapped = loop.create_future()
    loop.close()
    with patch.object(loop, "call_soon_threadsafe", autospec=True) as schedule:
        _cancel_wrapped_on_loop(wrapped, loop)
    schedule.assert_not_called()
    assert not wrapped.done()
    wrapped.cancel()


def test_loop_cancel_suppresses_only_the_ordinary_runtime_close_race() -> None:
    loop = asyncio.new_event_loop()
    wrapped = loop.create_future()
    try:
        with patch.object(loop, "call_soon_threadsafe", autospec=True, side_effect=RuntimeError("event loop is closed")) as schedule:
            _cancel_wrapped_on_loop(wrapped, loop)
        schedule.assert_called_once()
        assert not wrapped.done()
    finally:
        wrapped.cancel()
        loop.close()


@pytest.mark.parametrize("failure_kind", ["ordinary-other", "runtime-group", "custom-control-group"])
def test_loop_cancel_propagates_nonordinary_close_failures_by_identity(failure_kind: str) -> None:
    failure: BaseException
    if failure_kind == "ordinary-other":
        failure = ValueError("unexpected scheduling failure")
    elif failure_kind == "runtime-group":
        failure = ExceptionGroup("grouped close race", [RuntimeError("closed")])
    else:
        failure = _control_group()
    loop = asyncio.new_event_loop()
    wrapped = loop.create_future()
    try:
        with (
            patch.object(loop, "call_soon_threadsafe", autospec=True, side_effect=failure) as schedule,
            pytest.raises(type(failure)) as raised,
        ):
            _cancel_wrapped_on_loop(wrapped, loop)
        assert raised.value is failure
        schedule.assert_called_once()
        assert not wrapped.done()
    finally:
        wrapped.cancel()
        loop.close()
