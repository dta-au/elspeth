"""Owned adoption failure evidence survives cancellation and reaches durable terminal."""

from __future__ import annotations

import asyncio
import errno

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.web.coordination import lifecycle
from elspeth.web.sessions.composer_async_worker import _failure_leaves
from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN


def _failure(kind: str) -> BaseException:
    if kind == "sql":
        return OperationalError("CAS", {}, RuntimeError("private SQL failure"))
    if kind == "accounting":
        original = RuntimeError("private accounting failure")
        failure = ComposerOwnedSettlementFailure()
        failure.__cause__ = original
        return failure
    if kind == "disk":
        return OSError(errno.ENOSPC, "private disk failure")
    if kind == "nested":
        return ExceptionGroup("owned nested failures", [ValueError("ordinary"), _failure("sql"), _failure("accounting")])
    return OSError(errno.ENOENT, "ordinary missing file")


@pytest.mark.parametrize("kind", ("sql", "accounting", "disk", "nested", "ordinary"))
def test_lifecycle_preserves_nominal_secondary_priority_without_promoting_ordinary(kind: str) -> None:
    secondary = _failure(kind)
    primary = RuntimeError("ordinary renewal failure")
    escaped = lifecycle._preserve_failures(primary, (secondary,), note_prefix="close", group_message="owned close")
    assert lifecycle._has_required_lifecycle_failure(escaped) is (kind != "ordinary")
    if kind != "ordinary":
        assert any(leaf is secondary for leaf in _failure_leaves(escaped)) or isinstance(secondary, BaseExceptionGroup)
    else:
        assert escaped is primary


@pytest.mark.parametrize("kind", ("sql", "accounting", "disk", "nested", "ordinary"))
def test_cancelled_adoption_preserves_nominal_failure_instances(kind: str) -> None:
    failure = _failure(kind)
    cancelled = asyncio.CancelledError(COMPOSER_SHUTDOWN)
    escaped = lifecycle._failure_after_cancellation(cancelled, (("adoption SQL", failure),), group_message="owned adoption")
    assert lifecycle._has_required_lifecycle_failure(escaped) is (kind != "ordinary")
    assert escaped is (cancelled if kind == "ordinary" else failure)
