"""Static contract tests for the shipped Docker Compose deployment bundle."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yaml"
POSTGRES_COMPOSE = REPO_ROOT / "deploy" / "compose" / "postgres.yaml"
WEB_COMPOSE = REPO_ROOT / "deploy" / "compose" / "web-postgres.yaml"
POSTGRES_INIT = REPO_ROOT / "deploy" / "compose" / "postgres-init.sql"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "compose" / ".env.example"

ELSPETH_IMAGE = "${REGISTRY:-ghcr.io/johnm-dta}/elspeth:${IMAGE_TAG:?set IMAGE_TAG to an immutable sha-* or v* tag}"
POSTGRES_PASSWORD_REF = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"


def _load(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _service(document: dict[str, Any], name: str) -> dict[str, Any]:
    service = document["services"][name]
    assert isinstance(service, dict)
    return service


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict), "Compose environments must use explicit key/value mappings"
    return {str(key): str(value) for key, value in environment.items()}


def _volume_targets(service: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            targets.append(volume.split(":", maxsplit=2)[1])
        else:
            targets.append(str(volume["target"]))
    return targets


def test_base_is_cli_first_sqlite_with_an_immutable_elspeth_image() -> None:
    document = _load(BASE_COMPOSE)
    service = _service(document, "elspeth")

    assert service["image"] == ELSPETH_IMAGE
    assert service["command"] == ["--help"]
    assert _environment(service)["DATABASE_URL"].startswith("${DATABASE_URL:-sqlite:")
    assert "postgres" not in document["services"]


def test_no_compose_artifact_defaults_to_latest() -> None:
    for path in (BASE_COMPOSE, POSTGRES_COMPOSE, WEB_COMPOSE):
        text = path.read_text(encoding="utf-8")
        assert ":-latest" not in text
        assert re.search(r"(?:^|[/:])latest(?:$|[\s\"'}])", text, re.MULTILINE) is None


def test_postgresql_overlay_wires_a_healthy_persistent_server_and_cli_database() -> None:
    document = _load(POSTGRES_COMPOSE)
    postgres = _service(document, "postgres")
    cli = _service(document, "elspeth")

    assert postgres["image"] == "postgres:16-alpine"
    assert "profiles" not in postgres
    assert postgres["healthcheck"]["test"]
    assert "pg_isready" in " ".join(postgres["healthcheck"]["test"])
    assert "postgres_data" in document["volumes"]
    assert any(volume.startswith("postgres_data:") and "/var/lib/postgresql/data" in volume for volume in postgres["volumes"])
    assert any("postgres-init.sql" in volume and "/docker-entrypoint-initdb.d/" in volume for volume in postgres["volumes"])
    assert _environment(postgres)["POSTGRES_PASSWORD"] == POSTGRES_PASSWORD_REF

    database_url = _environment(cli)["DATABASE_URL"]
    assert database_url == ("postgresql+psycopg://elspeth:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}@postgres:5432/elspeth_landscape")
    assert cli["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_postgresql_initializer_creates_exactly_the_two_application_databases() -> None:
    sql = POSTGRES_INIT.read_text(encoding="utf-8")
    created = re.findall(r"(?im)^\s*CREATE\s+DATABASE\s+([a-z_][a-z0-9_]*)\b", sql)

    assert created == ["elspeth_sessions", "elspeth_landscape"]


def test_state_initializer_repairs_all_private_state_directories_then_exits() -> None:
    document = _load(WEB_COMPOSE)
    state_init = _service(document, "state-init")
    command = state_init["command"]

    assert state_init["image"] == ELSPETH_IMAGE
    assert state_init["user"] == "1000:1000"
    assert state_init["entrypoint"] == ["/bin/sh", "-c"]
    assert isinstance(command, list)
    assert len(command) == 1, "/bin/sh -c must receive the complete install expression as one argument"
    assert "install -d -m 0700" in command[0]
    assert "/app/state/data" in command[0]
    assert "/app/state/data/blobs" in command[0]
    assert "/app/state/payloads" in command[0]
    assert _volume_targets(state_init).count("/app/state") == 1
    assert state_init["restart"] == "no"


def test_web_service_uses_distinct_external_postgresql_state_and_one_persistent_worker() -> None:
    document = _load(WEB_COMPOSE)
    web = _service(document, "web")
    environment = _environment(web)

    assert web["image"] == ELSPETH_IMAGE
    assert web["command"] == ["web", "--host", "0.0.0.0", "--port", "8451"]
    assert environment["ELSPETH_WEB__DEPLOYMENT_TARGET"] == "docker-compose"
    assert environment["ELSPETH_WEB__DEPLOYMENT_STATE_MODE"] == "external-postgresql"
    assert environment["ELSPETH_WEB__SESSION_DB_URL"] == (
        "postgresql+psycopg://elspeth:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}@postgres:5432/elspeth_sessions"
    )
    assert environment["ELSPETH_WEB__LANDSCAPE_URL"] == (
        "postgresql+psycopg://elspeth:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}@postgres:5432/elspeth_landscape"
    )
    assert environment["ELSPETH_WEB__SESSION_DB_URL"] != environment["ELSPETH_WEB__LANDSCAPE_URL"]
    assert environment["ELSPETH_WEB__DATA_DIR"] == "/app/state/data"
    assert environment["ELSPETH_WEB__PAYLOAD_STORE_PATH"] == "/app/state/payloads"
    assert environment["WEB_CONCURRENCY"] == "1"
    assert web["deploy"]["replicas"] == 1
    assert web["ports"] == ["8451:8451"]
    assert _volume_targets(web).count("/app/state") == 1
    assert "elspeth_state" in document["volumes"]


def test_web_readiness_probe_uses_only_python_standard_library() -> None:
    web = _service(_load(WEB_COMPOSE), "web")
    probe = " ".join(web["healthcheck"]["test"])

    assert "python" in probe
    assert "urllib.request" in probe
    assert "http://127.0.0.1:8451/api/ready" in probe
    assert "curl" not in probe


def test_web_initialization_is_ordered_before_the_web_process() -> None:
    document = _load(WEB_COMPOSE)
    web_init = _service(document, "web-init")
    web = _service(document, "web")

    assert web_init["command"] == ["doctor", "deployment", "--init-schema"]
    assert web_init["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert web_init["depends_on"]["state-init"]["condition"] == "service_completed_successfully"
    assert _volume_targets(web_init).count("/app/state") == 1
    assert web["depends_on"] == {"web-init": {"condition": "service_completed_successfully"}}


def test_web_overlay_sets_production_composer_defaults() -> None:
    environment = _environment(_service(_load(WEB_COMPOSE), "web"))

    assert environment["ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED"] == ("${ELSPETH_WEB_COMPOSER_BOOT_PROBE_ENABLED:-true}")
    assert environment["ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS"] == "15"
    assert environment["ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS"] == "10"
    assert environment["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"] == "85"
    assert environment["ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE"] == "10"


def test_example_environment_requires_url_safe_generated_password_and_no_secrets() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assignments = {
        key: value
        for raw_line in text.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
        for key, value in [line.split("=", maxsplit=1)]
    }

    assert "openssl rand -hex 24" in text
    assert "48-character lowercase hexadecimal" in text
    assert re.search(r"^[0-9a-f]{48}$", "0" * 48)
    for name in (
        "POSTGRES_PASSWORD",
        "ELSPETH_WEB_SECRET_KEY",
        "ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "AZURE_CONTENT_SAFETY_KEY",
    ):
        assert name in assignments
        assert assignments[name] == ""


def test_supported_three_file_compose_bundle_renders() -> None:
    environment = os.environ | {
        "IMAGE_TAG": "sha-test",
        "POSTGRES_PASSWORD": "0123456789abcdef0123456789abcdef0123456789abcdef",
        "ELSPETH_WEB_SECRET_KEY": "test-only-secret-key-with-more-than-32-bytes",
        "ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yaml",
            "-f",
            "deploy/compose/postgres.yaml",
            "-f",
            "deploy/compose/web-postgres.yaml",
            "config",
            "--quiet",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
