"""Real PostgreSQL proof for the provider-neutral external-state contract.

The target parameterization below verifies ELSPETH's runtime configuration
contract.  It is deliberately not evidence that a maintained deployment bundle
exists for every named target.  Run the focused suite sequentially with
``CI=1 uv run --frozen pytest -q -n 0 -m testcontainer``; parallel workers are
rejected before Docker starts so the suite cannot fan out containers.  The
complete copy-paste command is ``_SEQUENTIAL_TEST_COMMAND`` in the sibling
``conftest.py``.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from click.testing import Result
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import SecretBytes
from sqlalchemy import create_engine, inspect, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ProgrammingError
from typer.testing import CliRunner

from elspeth.cli import app as cli_app
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.external_state_startup import ExternalStateSchemaNotReadyError
from elspeth.web.schema_probe import (
    SchemaState,
    init_landscape_schema,
    init_session_schema,
    probe_landscape_schema,
    probe_session_schema,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from elspeth.web.sessions.models import schema_identity_table as session_schema_identity_table

pytestmark = pytest.mark.testcontainer

_SAFE_IDENTIFIER = re.compile(r"[a-z0-9_]+\Z")
_RUNTIME_CONTRACT_TARGETS = (
    "docker-compose",
    "linux-systemd",
    "aws-ecs",
    "azure-container-apps",
    "kubernetes",
)


def test_sequential_execution_contract_is_guarded_and_documented() -> None:
    shared_fixtures = importlib.import_module("tests.testcontainer.web.conftest")

    class _WorkerConfig:
        def __init__(self) -> None:
            self.workerinput: dict[str, object] = {}

    class _WorkerRequest:
        def __init__(self) -> None:
            self.config = _WorkerConfig()

    with pytest.raises(pytest.UsageError, match=r"-n 0 -m testcontainer") as exc_info:
        shared_fixtures._require_sequential_postgres_acceptance(cast(pytest.FixtureRequest, _WorkerRequest()))
    assert shared_fixtures._SEQUENTIAL_TEST_COMMAND in str(exc_info.value)

    plan_path = Path(__file__).parents[3] / "docs/superpowers/plans/2026-07-24-cross-platform-deployment-contract.md"
    task12 = plan_path.read_text(encoding="utf-8").split("### Task 12:", maxsplit=1)[1]
    focused_command = task12.split("###", maxsplit=1)[0].split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]

    assert shared_fixtures._SEQUENTIAL_TEST_COMMAND in focused_command

    aws_startup_tests = importlib.import_module("tests.testcontainer.web.test_aws_ecs_validate_only_startup")
    aws_doctor_tests = importlib.import_module("tests.testcontainer.web.test_doctor_aws_ecs_postgres")
    assert "external_deployment_postgres_url" in vars(shared_fixtures)
    assert "pytest_plugins" not in vars(aws_startup_tests)
    assert "pytest_plugins" not in vars(aws_doctor_tests)


def _identifier(prefix: str) -> str:
    value = f"{prefix}_{uuid.uuid4().hex}"
    assert _SAFE_IDENTIFIER.fullmatch(value)
    return value


def _render_url(
    base_url: str | URL,
    *,
    database: str,
    username: str | None = None,
    password: str | None = None,
) -> str:
    parsed = make_url(base_url).set(database=database)
    if username is not None:
        parsed = parsed.set(username=username, password=password)
    return parsed.render_as_string(hide_password=False)


def _psycopg_connect(url: str) -> psycopg.Connection[Any]:
    parsed = make_url(url)
    assert parsed.host is not None
    assert parsed.username is not None
    assert parsed.database is not None
    return psycopg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        dbname=parsed.database,
        user=parsed.username,
        password=parsed.password,
        autocommit=True,
    )


@dataclass(slots=True)
class _DatabasePair:
    postgres_url: str
    session_database: str
    landscape_database: str
    runtime_role: str
    runtime_password: str
    role_created: bool = False

    @property
    def session_owner_url(self) -> str:
        return _render_url(self.postgres_url, database=self.session_database)

    @property
    def landscape_owner_url(self) -> str:
        return _render_url(self.postgres_url, database=self.landscape_database)

    @property
    def session_runtime_url(self) -> str:
        assert self.role_created
        return _render_url(
            self.postgres_url,
            database=self.session_database,
            username=self.runtime_role,
            password=self.runtime_password,
        )

    @property
    def landscape_runtime_url(self) -> str:
        assert self.role_created
        return _render_url(
            self.postgres_url,
            database=self.landscape_database,
            username=self.runtime_role,
            password=self.runtime_password,
        )

    def provision_runtime_role(self) -> None:
        assert self.role_created is False
        with _psycopg_connect(self.postgres_url) as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(self.runtime_role),
                    sql.Literal(self.runtime_password),
                )
            )
            for database in (self.session_database, self.landscape_database):
                admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database),
                        sql.Identifier(self.runtime_role),
                    )
                )

        self.role_created = True
        self.grant_runtime_read_permissions()

    def grant_runtime_read_permissions(self) -> None:
        assert self.role_created
        for owner_url in (self.session_owner_url, self.landscape_owner_url):
            with _psycopg_connect(owner_url) as owner:
                owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                owner.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(self.runtime_role)))
                owner.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(self.runtime_role)))


@pytest.fixture
def database_pair(external_deployment_postgres_url: str) -> Iterator[_DatabasePair]:
    databases = _DatabasePair(
        postgres_url=external_deployment_postgres_url,
        session_database=_identifier("external_session"),
        landscape_database=_identifier("external_landscape"),
        runtime_role=_identifier("external_runtime"),
        runtime_password=f"external-runtime-{uuid.uuid4().hex}",
    )
    assert databases.session_database != databases.landscape_database
    with _psycopg_connect(external_deployment_postgres_url) as admin:
        for database in (databases.session_database, databases.landscape_database):
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    try:
        yield databases
    finally:
        with _psycopg_connect(external_deployment_postgres_url) as admin:
            for database in (databases.session_database, databases.landscape_database):
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            if databases.role_created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(databases.runtime_role)))


@pytest.fixture(autouse=True)
def _clear_inherited_web_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("ELSPETH_WEB__"):
            monkeypatch.delenv(key, raising=False)


def _prepare_directories(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    payload_dir = tmp_path / "payloads"
    for directory in (data_dir, data_dir / "blobs", payload_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    return data_dir, payload_dir


def _aws_settings() -> dict[str, str]:
    return {
        "operator_telemetry": "aws-otlp",
        "operator_telemetry_environment": "test",
        "operator_telemetry_release": "git-test",
        "operator_telemetry_ecs_cluster": "elspeth-test",
        "operator_telemetry_ecs_service": "elspeth-web",
        "operator_telemetry_task_definition_family": "elspeth-web-task",
        "operator_telemetry_task_definition_revision": "1",
    }


def _settings(
    tmp_path: Path,
    *,
    target: str,
    session_url: str,
    landscape_url: str,
) -> WebSettings:
    data_dir, payload_dir = _prepare_directories(tmp_path)
    target_settings = _aws_settings() if target == "aws-ecs" else {}
    return WebSettings(
        deployment_target=target,  # type: ignore[arg-type]
        deployment_state_mode="external-postgresql",
        host="0.0.0.0",
        data_dir=data_dir,
        payload_store_path=payload_dir,
        session_db_url=session_url,
        landscape_url=landscape_url,
        secret_key="external-deployment-secret-key-with-more-than-32-bytes",
        shareable_link_signing_key=SecretBytes(bytes(range(32))),
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_boot_probe_enabled=False,
        **target_settings,  # type: ignore[arg-type]
    )


def _doctor_environment(
    tmp_path: Path,
    *,
    target: str,
    session_url: str,
    landscape_url: str,
) -> dict[str, str]:
    data_dir, payload_dir = _prepare_directories(tmp_path)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ELSPETH_WEB__")}
    environment.update(
        {
            "ELSPETH_WEB__DEPLOYMENT_TARGET": target,
            "ELSPETH_WEB__DEPLOYMENT_STATE_MODE": "external-postgresql",
            "ELSPETH_WEB__SESSION_DB_URL": session_url,
            "ELSPETH_WEB__LANDSCAPE_URL": landscape_url,
            "ELSPETH_WEB__DATA_DIR": str(data_dir),
            "ELSPETH_WEB__PAYLOAD_STORE_PATH": str(payload_dir),
            "ELSPETH_WEB__HOST": "0.0.0.0",
            "ELSPETH_WEB__SECRET_KEY": "doctor-deployment-secret-key-with-more-than-32-bytes",
            "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode("ascii"),
            "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": "15",
            "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": "10",
            "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": "85.0",
            "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
            "ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED": "false",
        }
    )
    if target == "aws-ecs":
        environment.update(
            {
                "ELSPETH_WEB__OPERATOR_TELEMETRY": "aws-otlp",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_SERVICE_NAME": "elspeth-web",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_ENVIRONMENT": "test",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE": "git-test",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_CLUSTER": "elspeth-test",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_SERVICE": "elspeth-web",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY": "elspeth-web-task",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION": "1",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_EXPORT_INTERVAL_SECONDS": "60",
                "ELSPETH_WEB__OPERATOR_PIPELINE_TELEMETRY_GRANULARITY": "lifecycle",
            }
        )
    return environment


def _invoke_doctor(environment: dict[str, str], *, init_schema: bool) -> Result:
    arguments = ["--no-dotenv", "doctor", "deployment"]
    if init_schema:
        arguments.append("--init-schema")
    arguments.append("--json")
    return CliRunner().invoke(cli_app, arguments, env=environment)


def _assert_all_green_report(stdout: str) -> None:
    payload = json.loads(stdout)
    assert isinstance(payload, list)
    assert payload
    assert all(isinstance(item, dict) and item.get("ok") is True for item in payload)
    names = [item["name"] for item in payload]
    assert len(names) == len(set(names))
    assert names[-2:] == ["session_schema", "landscape_schema"]


def _assert_redacted(rendered: str, databases: _DatabasePair) -> None:
    assert databases.runtime_password not in rendered
    assert databases.session_runtime_url not in rendered
    assert databases.landscape_runtime_url not in rendered


def _assert_ddl_denied(url: str) -> None:
    engine = create_engine(url)
    try:
        with pytest.raises(ProgrammingError) as exc_info, engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE ddl_must_be_denied (id integer primary key)")
        assert isinstance(exc_info.value.orig, psycopg.errors.InsufficientPrivilege)
        assert exc_info.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", _RUNTIME_CONTRACT_TARGETS)
def test_external_target_doctor_initializes_then_runtime_stays_validate_only(
    tmp_path: Path,
    database_pair: _DatabasePair,
    target: str,
) -> None:
    environment = _doctor_environment(
        tmp_path,
        target=target,
        session_url=database_pair.session_owner_url,
        landscape_url=database_pair.landscape_owner_url,
    )

    doctor_result = _invoke_doctor(environment, init_schema=True)

    assert doctor_result.exit_code == 0, doctor_result.output
    _assert_all_green_report(doctor_result.stdout)
    initialized_session = create_session_engine(database_pair.session_owner_url)
    initialized_landscape = create_engine(database_pair.landscape_owner_url)
    try:
        assert probe_session_schema(initialized_session) is SchemaState.CURRENT
        assert probe_landscape_schema(initialized_landscape) is SchemaState.CURRENT
    finally:
        initialized_session.dispose()
        initialized_landscape.dispose()

    database_pair.provision_runtime_role()
    _assert_ddl_denied(database_pair.session_runtime_url)
    _assert_ddl_denied(database_pair.landscape_runtime_url)
    settings = _settings(
        tmp_path,
        target=target,
        session_url=database_pair.session_runtime_url,
        landscape_url=database_pair.landscape_runtime_url,
    )
    session_owner = create_session_engine(database_pair.session_owner_url)
    landscape_owner = create_engine(database_pair.landscape_owner_url)
    try:
        before_session = tuple(sorted(inspect(session_owner).get_table_names()))
        before_landscape = tuple(sorted(inspect(landscape_owner).get_table_names()))
        web_app = create_app(settings)
        with TestClient(web_app) as client:
            response = client.get("/api/ready")

        assert response.status_code == 200, response.text
        assert response.json()["ready"] is True
        assert tuple(sorted(inspect(session_owner).get_table_names())) == before_session
        assert tuple(sorted(inspect(landscape_owner).get_table_names())) == before_landscape
    finally:
        session_owner.dispose()
        landscape_owner.dispose()


def test_doctor_rejects_same_database_without_leaking_credentials(
    tmp_path: Path,
    database_pair: _DatabasePair,
) -> None:
    database_pair.provision_runtime_role()
    same_url = database_pair.session_runtime_url
    environment = _doctor_environment(
        tmp_path,
        target="docker-compose",
        session_url=same_url,
        landscape_url=same_url,
    )

    result = _invoke_doctor(environment, init_schema=True)

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    by_name = {item["name"]: item for item in report}
    assert by_name["separate_db_targets"]["ok"] is False
    _assert_redacted(result.stdout + result.stderr, database_pair)
    owner = create_session_engine(database_pair.session_owner_url)
    try:
        assert inspect(owner).get_table_names() == []
    finally:
        owner.dispose()


def test_validate_only_startup_rejects_missing_and_stale_schema_without_leaking_credentials(
    tmp_path: Path,
    database_pair: _DatabasePair,
) -> None:
    session_owner = create_session_engine(database_pair.session_owner_url)
    landscape_owner = create_engine(database_pair.landscape_owner_url)
    try:
        init_landscape_schema(landscape_owner)
        database_pair.provision_runtime_role()
        settings = _settings(
            tmp_path,
            target="azure-container-apps",
            session_url=database_pair.session_runtime_url,
            landscape_url=database_pair.landscape_runtime_url,
        )

        with pytest.raises(ExternalStateSchemaNotReadyError, match="session_schema") as exc_info:
            create_app(settings)

        _assert_redacted(repr(exc_info.value), database_pair)
        assert probe_session_schema(session_owner) is SchemaState.MISSING

        init_session_schema(session_owner)
        database_pair.grant_runtime_read_permissions()
        with session_owner.begin() as connection:
            connection.execute(update(session_schema_identity_table).values(schema_epoch=SESSION_SCHEMA_EPOCH - 1))
        assert probe_session_schema(session_owner) is SchemaState.STALE

        with pytest.raises(ExternalStateSchemaNotReadyError, match="session_schema") as exc_info:
            create_app(settings)

        _assert_redacted(repr(exc_info.value), database_pair)
        assert probe_session_schema(session_owner) is SchemaState.STALE
        assert probe_landscape_schema(landscape_owner) is SchemaState.CURRENT
    finally:
        session_owner.dispose()
        landscape_owner.dispose()


def test_doctor_init_schema_fails_cleanly_when_initializer_cannot_run_ddl(
    tmp_path: Path,
    database_pair: _DatabasePair,
) -> None:
    database_pair.provision_runtime_role()
    environment = _doctor_environment(
        tmp_path,
        target="linux-systemd",
        session_url=database_pair.session_runtime_url,
        landscape_url=database_pair.landscape_runtime_url,
    )

    result = _invoke_doctor(environment, init_schema=True)

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    by_name = {item["name"]: item for item in report}
    assert by_name["session_schema"]["ok"] is False
    assert by_name["landscape_schema"]["ok"] is False
    assert "initialization failed (ProgrammingError)" in by_name["session_schema"]["detail"]
    assert "initialization failed (ProgrammingError)" in by_name["landscape_schema"]["detail"]
    _assert_redacted(result.stdout + result.stderr, database_pair)
    session_owner = create_session_engine(database_pair.session_owner_url)
    landscape_owner = create_engine(database_pair.landscape_owner_url)
    try:
        assert probe_session_schema(session_owner) is SchemaState.MISSING
        assert probe_landscape_schema(landscape_owner) is SchemaState.MISSING
    finally:
        session_owner.dispose()
        landscape_owner.dispose()
