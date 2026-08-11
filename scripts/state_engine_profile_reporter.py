"""Pytest reporter for runtime-observed state-engine execution profiles.

The assessment manifest never supplies backend facts to this plugin. A trusted
test passes the live database connection at the runtime boundary; the reporter
queries that connection and emits the resulting dialect/version observation.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Connection

SQLITE_DEPLOYMENTS = {
    "single-process-leader",
    "same-host-leader-plus-claim-only-followers",
    "web-hosted-leader-plus-same-host-cli-followers",
}
POSTGRESQL_DEPLOYMENT = "aws-single-leader-landscape"
PROFILE_STATE_KEY = pytest.StashKey["ProfileState"]()


@dataclass(frozen=True)
class ProfileObservation:
    """Facts observed at a trusted test/runtime database boundary."""

    profile_case_id: str
    state_store: str
    deployment: str
    backend_version: str
    backend_probe: dict[str, str]
    probe_node_id: str


@dataclass
class ProfileState:
    """Per-pytest-process reporter state."""

    node_ids: list[str] = field(default_factory=list)
    observation: ProfileObservation | None = None


class RuntimeProfileReporter:
    """Fixture API used by a trusted test to report its live backend boundary."""

    def __init__(self, state: ProfileState, node_id: str) -> None:
        self._state = state
        self._node_id = node_id

    def _record(self, observation: ProfileObservation) -> None:
        prior = self._state.observation
        if prior is not None and prior != observation:
            raise AssertionError("one pytest evidence run cannot report multiple execution profiles")
        self._state.observation = observation

    def observe_sqlite(self, connection: sqlite3.Connection, *, deployment: str) -> None:
        """Query the supplied live SQLite connection and retain its observed version."""

        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("SQLite profile observation requires sqlite3.Connection")
        if deployment not in SQLITE_DEPLOYMENTS:
            raise ValueError(f"unsupported SQLite deployment: {deployment}")
        row = connection.execute("SELECT sqlite_version()").fetchone()
        if row is None or not isinstance(row[0], str) or re.fullmatch(r"3\.\d+(?:\.\d+)?", row[0]) is None:
            raise AssertionError("SQLite runtime probe did not return a 3.x version")
        self._record(
            ProfileObservation(
                profile_case_id=f"sqlite-wal-{deployment}",
                state_store="sqlite-wal",
                deployment=deployment,
                backend_version=row[0],
                backend_probe={
                    "kind": "sqlite-connection-query",
                    "dialect": "sqlite",
                    "query": "SELECT sqlite_version()",
                },
                probe_node_id=self._node_id,
            )
        )

    def observe_postgresql(self, connection: Connection, *, deployment: str) -> None:
        """Query a live SQLAlchemy PostgreSQL connection for its server version."""

        if not isinstance(connection, Connection):
            raise TypeError("PostgreSQL profile observation requires sqlalchemy.engine.Connection")
        if connection.dialect.name != "postgresql":
            raise ValueError("PostgreSQL profile observation requires the postgresql dialect")
        if deployment != POSTGRESQL_DEPLOYMENT:
            raise ValueError(f"unsupported PostgreSQL deployment: {deployment}")
        value = connection.exec_driver_sql("SHOW server_version").scalar_one()
        if not isinstance(value, str) or re.fullmatch(r"16(?:\.\d+){0,2}", value) is None:
            raise AssertionError("PostgreSQL runtime probe did not return a 16.x server version")
        self._record(
            ProfileObservation(
                profile_case_id="postgresql-16-aws-single-leader-landscape",
                state_store="postgresql-16",
                deployment=deployment,
                backend_version=value,
                backend_probe={
                    "kind": "postgresql-server-query",
                    "dialect": "postgresql",
                    "query": "SHOW server_version",
                },
                probe_node_id=self._node_id,
            )
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("state-engine-profile")
    group.addoption(
        "--state-engine-profile-report",
        action="store",
        help="Repository-relative JSON path for the runtime-observed profile report.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[PROFILE_STATE_KEY] = ProfileState()


def pytest_collection_finish(session: pytest.Session) -> None:
    session.config.stash[PROFILE_STATE_KEY].node_ids = [item.nodeid for item in session.items]


def pytest_runtest_setup(item: pytest.Item) -> None:
    item.user_properties.append(("elspeth_node_id", item.nodeid))


@pytest.fixture
def state_engine_profile(request: pytest.FixtureRequest) -> RuntimeProfileReporter:
    """Return the runtime-bound reporter for the currently executing test node."""

    return RuntimeProfileReporter(request.config.stash[PROFILE_STATE_KEY], request.node.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    report_option: Any = session.config.getoption("state_engine_profile_report")
    if report_option is None:
        return
    state = session.config.stash[PROFILE_STATE_KEY]
    observation = state.observation
    if observation is None:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    if observation.probe_node_id not in state.node_ids:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    document = {
        "schema_version": 1,
        "producer": "elspeth-state-engine-profile-reporter-v1",
        "profile_case_id": observation.profile_case_id,
        "state_store": observation.state_store,
        "deployment": observation.deployment,
        "backend_version": observation.backend_version,
        "backend_probe": observation.backend_probe,
        "deployment_probe": {
            "kind": "trusted-test-runtime-assertion",
            "node_id": observation.probe_node_id,
        },
        "probe_node_id": observation.probe_node_id,
        "node_ids": state.node_ids,
    }
    report_path = Path(report_option)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
