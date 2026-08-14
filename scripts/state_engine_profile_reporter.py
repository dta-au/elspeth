"""Pytest reporter for runtime-observed state-engine execution profiles.

The assessment manifest never supplies backend facts to this plugin. A trusted
test passes the live database connection at the runtime boundary; the reporter
queries that connection and emits the resulting dialect/version observation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from sqlalchemy.engine import Connection

SQLITE_DEPLOYMENTS = {
    "single-process-leader",
    "same-host-leader-plus-claim-only-followers",
    "web-hosted-leader-plus-same-host-cli-followers",
}
POSTGRESQL_DEPLOYMENT = "aws-single-leader-landscape"
PROFILE_STATE_KEY = pytest.StashKey["ProfileState"]()
MISSING_EXTERNAL_FIELD = object()
Outcome = Literal["passed", "failed", "error", "skipped", "xfailed", "xpassed"]
ReportPhase = Literal["setup", "call", "teardown"]
WarningPhase = Literal["config", "collect", "runtest"]
OUTCOME_PRIORITY: dict[Outcome, int] = {
    "passed": 0,
    "skipped": 1,
    "xfailed": 2,
    "xpassed": 3,
    "failed": 4,
    "error": 5,
}


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
    outcomes_by_node: dict[str, Outcome] = field(default_factory=dict)
    warnings: list[WarningObservation] = field(default_factory=list)


@dataclass(frozen=True)
class TestReportObservation:
    """Owned admission of the external pytest TestReport fields we consume."""

    node_id: str
    phase: ReportPhase
    outcome: Literal["passed", "failed", "skipped"]
    expected_failure: bool


@dataclass(frozen=True)
class WarningObservation:
    """Owned warning provenance admitted from pytest's WarningMessage."""

    node_id: str | None
    when: WarningPhase
    category: str
    message: str
    filename: str
    line: int


def _admit_test_report(report: pytest.TestReport) -> TestReportObservation:
    fields = vars(report)
    node_id = fields.get("nodeid")
    phase = fields.get("when")
    outcome = fields.get("outcome")
    if not isinstance(node_id, str) or not node_id:
        raise TypeError("pytest TestReport nodeid must be non-empty text")
    if phase not in {"setup", "call", "teardown"}:
        raise TypeError("pytest TestReport when must be setup, call, or teardown")
    if outcome not in {"passed", "failed", "skipped"}:
        raise TypeError("pytest TestReport outcome must be passed, failed, or skipped")
    wasxfail = fields.get("wasxfail", MISSING_EXTERNAL_FIELD)
    if wasxfail is not MISSING_EXTERNAL_FIELD and not isinstance(wasxfail, str):
        raise TypeError("pytest TestReport wasxfail must be text when present")
    longrepr = fields.get("longrepr")
    strict_xpass = outcome == "failed" and isinstance(longrepr, str) and longrepr.startswith("[XPASS(strict)]")
    return TestReportObservation(
        node_id=node_id,
        phase=phase,
        outcome=outcome,
        expected_failure=wasxfail is not MISSING_EXTERNAL_FIELD or strict_xpass,
    )


def _classify_test_report(report: TestReportObservation) -> Outcome | None:
    if report.expected_failure:
        return "xfailed" if report.outcome == "skipped" else "xpassed"
    if report.outcome == "skipped":
        return "skipped"
    if report.phase != "call":
        return "error" if report.outcome == "failed" else None
    return "failed" if report.outcome == "failed" else "passed"


def _admit_warning(
    warning_message: warnings.WarningMessage,
    when: str,
    node_id: str,
) -> WarningObservation:
    fields = vars(warning_message)
    category = fields.get("category")
    message = fields.get("message")
    filename = fields.get("filename")
    line = fields.get("lineno")
    if when not in {"config", "collect", "runtest"}:
        raise TypeError("pytest warning phase must be config, collect, or runtest")
    if not isinstance(node_id, str):
        raise TypeError("pytest warning nodeid must be text")
    if not isinstance(category, type) or not issubclass(category, Warning):
        raise TypeError("pytest warning category must be a Warning type")
    if not isinstance(message, Warning):
        raise TypeError("pytest warning message must be a Warning instance")
    if not isinstance(filename, str) or not filename:
        raise TypeError("pytest warning filename must be non-empty text")
    if type(line) is not int or line <= 0:
        raise TypeError("pytest warning line must be a positive integer")
    return WarningObservation(
        node_id=node_id or None,
        when=cast(WarningPhase, when),
        category=f"{category.__module__}.{category.__qualname__}",
        message=str(message),
        filename=filename,
        line=line,
    )


class ProfileEventRecorder:
    """Capture machine outcomes and warnings for one pytest session."""

    def __init__(self, state: ProfileState) -> None:
        self._state = state

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        admitted = _admit_test_report(report)
        outcome = _classify_test_report(admitted)
        if outcome is None:
            return
        prior = self._state.outcomes_by_node.get(admitted.node_id)
        if prior is None or OUTCOME_PRIORITY[outcome] > OUTCOME_PRIORITY[prior]:
            self._state.outcomes_by_node[admitted.node_id] = outcome

    def pytest_warning_recorded(
        self,
        warning_message: warnings.WarningMessage,
        when: str,
        nodeid: str,
        location: tuple[str, int, str] | None,
    ) -> None:
        del location
        self._state.warnings.append(_admit_warning(warning_message, when, nodeid))


class RuntimeProfileReporter:
    """Fixture API used by a trusted test to report its live backend boundary."""

    def __init__(self, state: ProfileState, node_id: str) -> None:
        self._state = state
        self._node_id = node_id

    def _record(self, observation: ProfileObservation) -> None:
        prior = self._state.observation
        if prior is not None:
            # A selector lane legitimately contains several trusted probe
            # tests (deployment-profile probes, process-death matrices, the
            # follower join/drain suite). The single-observation invariant
            # protects against one run claiming two DIFFERENT profiles, not
            # against repeated agreement, so a later probe must match the
            # first on every profile-identity field; the first probe stays
            # bound as the report's deployment_probe node.
            if replace(observation, probe_node_id=prior.probe_node_id) != prior:
                raise AssertionError("one pytest evidence run cannot report two disagreeing execution profiles")
            return
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
    state = ProfileState()
    config.stash[PROFILE_STATE_KEY] = state
    config.pluginmanager.register(ProfileEventRecorder(state), "elspeth-state-engine-profile-events")


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
    if set(state.outcomes_by_node) != set(state.node_ids):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    document = {
        "schema_version": 2,
        "producer": "elspeth-state-engine-profile-reporter-v2",
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
        "outcomes": [{"node_id": node_id, "outcome": state.outcomes_by_node[node_id]} for node_id in state.node_ids],
        "warnings": [
            {
                "node_id": item.node_id,
                "when": item.when,
                "category": item.category,
                "message": item.message,
                "filename": item.filename,
                "line": item.line,
            }
            for item in state.warnings
        ],
    }
    report_path = Path(report_option)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
