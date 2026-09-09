"""Multi-probe semantics of the state-engine runtime profile reporter.

A selector lane legitimately contains several trusted probe tests (the
deployment-profile probes, the process-death matrices, the follower
join/drain suite), so one evidence run may record the same execution
profile more than once. The reporter's single-observation invariant
protects against a run claiming two DIFFERENT profiles; repeated
agreement is accepted with the first probe node staying bound as the
report's deployment_probe.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace

import pytest
from scripts.state_engine_profile_reporter import ProfileState, RuntimeProfileReporter


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def test_agreeing_probes_keep_the_first_probe_node(connection: sqlite3.Connection) -> None:
    state = ProfileState()
    first = RuntimeProfileReporter(state, "tests/x/test_a.py::test_first_probe")
    second = RuntimeProfileReporter(state, "tests/x/test_b.py::test_second_probe")

    first.observe_sqlite(connection, deployment="single-process-leader")
    second.observe_sqlite(connection, deployment="single-process-leader")

    observation = state.observation
    assert observation is not None
    assert observation.probe_node_id == "tests/x/test_a.py::test_first_probe"
    assert observation.profile_case_id == "sqlite-wal-single-process-leader"


def test_disagreeing_deployment_probes_fail_closed(connection: sqlite3.Connection) -> None:
    state = ProfileState()
    first = RuntimeProfileReporter(state, "tests/x/test_a.py::test_first_probe")
    second = RuntimeProfileReporter(state, "tests/x/test_b.py::test_second_probe")

    first.observe_sqlite(connection, deployment="single-process-leader")
    with pytest.raises(AssertionError, match="disagreeing execution profiles"):
        second.observe_sqlite(connection, deployment="same-host-leader-plus-claim-only-followers")

    observation = state.observation
    assert observation is not None
    assert observation.deployment == "single-process-leader"
    assert observation.probe_node_id == "tests/x/test_a.py::test_first_probe"


def test_any_identity_field_drift_fails_closed(connection: sqlite3.Connection) -> None:
    state = ProfileState()
    first = RuntimeProfileReporter(state, "tests/x/test_a.py::test_first_probe")
    first.observe_sqlite(connection, deployment="single-process-leader")
    observation = state.observation
    assert observation is not None

    drifted = replace(
        observation,
        backend_version="3.0.0",
        probe_node_id="tests/x/test_b.py::test_second_probe",
    )
    second = RuntimeProfileReporter(state, "tests/x/test_b.py::test_second_probe")
    with pytest.raises(AssertionError, match="disagreeing execution profiles"):
        second._record(drifted)
