"""Observed abandonment causes survive preflight races and CLI rendering."""

from pathlib import Path

import pytest

from elspeth.contracts.checkpoint import ResumeRefusalCause
from tests.fixtures.abandon_refusal_diagnostics import (
    assert_abandon_cas_refusal_cause,
    assert_abandon_execution_refusal_cause,
    assert_abandon_preflight_cause,
)


@pytest.mark.parametrize(
    "expected_cause",
    [ResumeRefusalCause.RUN_NOT_FOUND, ResumeRefusalCause.RUN_NOT_RUNNING, ResumeRefusalCause.LEADER_LIVE],
)
def test_abandon_preflight_emits_observed_cause(tmp_path: Path, expected_cause: ResumeRefusalCause) -> None:
    assert_abandon_preflight_cause(tmp_path, expected_cause)


@pytest.mark.parametrize("race_cause", [ResumeRefusalCause.RUN_NOT_RUNNING, ResumeRefusalCause.LEADER_LIVE])
def test_abandon_execution_refusal_keeps_cause_after_database_changes(tmp_path: Path, race_cause: ResumeRefusalCause) -> None:
    assert_abandon_execution_refusal_cause(tmp_path, race_cause)


def test_abandon_cas_refusal_keeps_cause_after_database_changes(tmp_path: Path) -> None:
    assert_abandon_cas_refusal_cause(tmp_path)
