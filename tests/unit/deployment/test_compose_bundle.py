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
POSTGRES_IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
POSTGRES_PASSWORD_REF = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
COMPOSE_TEST_ENVIRONMENT = os.environ | {
    "IMAGE_TAG": "sha-test",
    "POSTGRES_PASSWORD": "0123456789abcdef0123456789abcdef0123456789abcdef",
    "ELSPETH_WEB_SECRET_KEY": "test-only-secret-key-with-more-than-32-bytes",
    "ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
}


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


def _rendered_bundle() -> dict[str, Any]:
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
        ],
        cwd=REPO_ROOT,
        env=COMPOSE_TEST_ENVIRONMENT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)
    assert isinstance(rendered, dict)
    return rendered


def test_base_is_cli_first_sqlite_with_an_immutable_elspeth_image() -> None:
    document = _load(BASE_COMPOSE)
    service = _service(document, "elspeth")

    assert service["image"] == ELSPETH_IMAGE
    assert service["command"] == ["--help"]
    assert _environment(service)["DATABASE_URL"].startswith("${DATABASE_URL:-sqlite:")
    assert "postgres" not in document["services"]


def test_base_uses_named_volumes_for_writable_cli_paths() -> None:
    document = _load(BASE_COMPOSE)
    service = _service(document, "elspeth")

    assert "elspeth_output" in document["volumes"]
    assert "elspeth_cli_state" in document["volumes"]
    assert "elspeth_output:/app/output" in service["volumes"]
    assert "elspeth_cli_state:/app/state" in service["volumes"]
    assert not any(volume.startswith("./output:") or volume.startswith("./state:") for volume in service["volumes"])


def test_no_compose_artifact_defaults_to_latest() -> None:
    for path in (BASE_COMPOSE, POSTGRES_COMPOSE, WEB_COMPOSE):
        text = path.read_text(encoding="utf-8")
        assert ":-latest" not in text
        assert re.search(r"(?:^|[/:])latest(?:$|[\s\"'}])", text, re.MULTILINE) is None


def test_postgresql_overlay_wires_a_healthy_persistent_server_and_cli_database() -> None:
    document = _load(POSTGRES_COMPOSE)
    postgres = _service(document, "postgres")
    cli = _service(document, "elspeth")

    assert postgres["image"] == POSTGRES_IMAGE
    assert "profiles" not in postgres
    assert postgres["healthcheck"]["test"]
    health_command = " ".join(postgres["healthcheck"]["test"])
    assert "pg_isready" in health_command
    assert "--host 127.0.0.1" in health_command
    assert "-U elspeth" in health_command
    assert "-d postgres" in health_command
    assert "postgres_data" in document["volumes"]
    assert any(volume.startswith("postgres_data:") and "/var/lib/postgresql/data" in volume for volume in postgres["volumes"])
    assert any("postgres-init.sql" in volume and "/docker-entrypoint-initdb.d/" in volume for volume in postgres["volumes"])
    assert _environment(postgres)["POSTGRES_PASSWORD"] == POSTGRES_PASSWORD_REF
    assert postgres["restart"] == "unless-stopped"

    database_url = _environment(cli)["DATABASE_URL"]
    assert database_url == ("postgresql+psycopg://elspeth:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}@postgres:5432/elspeth_landscape")
    assert cli["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_postgresql_cli_waits_for_recurring_database_bootstrap() -> None:
    static_cli = _service(_load(POSTGRES_COMPOSE), "elspeth")
    rendered_cli = _service(_rendered_bundle(), "elspeth")

    assert static_cli["depends_on"]["postgres-bootstrap"]["condition"] == "service_completed_successfully"
    assert rendered_cli["depends_on"]["postgres-bootstrap"]["condition"] == "service_completed_successfully"


def test_postgresql_initializer_creates_exactly_the_two_application_databases() -> None:
    sql = POSTGRES_INIT.read_text(encoding="utf-8")
    created = re.findall(r"CREATE\s+DATABASE\s+([a-z_][a-z0-9_]*)\b", sql, re.IGNORECASE)

    assert created == ["elspeth_sessions", "elspeth_landscape"]
    assert sql.upper().count("WHERE NOT EXISTS") == 2
    assert sql.count("\\gexec") == 2


def test_postgresql_bootstrap_reconciles_reused_data_volumes_before_web_init() -> None:
    static = _load(POSTGRES_COMPOSE)
    bootstrap = _service(static, "postgres-bootstrap")

    assert bootstrap["image"] == POSTGRES_IMAGE
    assert bootstrap["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert _environment(bootstrap)["PGPASSWORD"] == POSTGRES_PASSWORD_REF
    assert bootstrap["command"] == [
        "psql",
        "--host",
        "postgres",
        "--username",
        "elspeth",
        "--dbname",
        "postgres",
        "--set",
        "ON_ERROR_STOP=1",
        "--file",
        "/bootstrap/postgres-init.sql",
    ]
    assert any("postgres-init.sql:/bootstrap/postgres-init.sql:ro" in volume for volume in bootstrap["volumes"])
    assert bootstrap["restart"] == "no"

    rendered = _rendered_bundle()
    rendered_bootstrap = _service(rendered, "postgres-bootstrap")
    rendered_web_init = _service(rendered, "web-init")
    rendered_postgres = _service(rendered, "postgres")
    assert "--host 127.0.0.1" in " ".join(rendered_postgres["healthcheck"]["test"])
    assert rendered_bootstrap["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert rendered_web_init["depends_on"]["postgres-bootstrap"]["condition"] == "service_completed_successfully"


def test_postgresql_dependency_images_are_digest_pinned_in_source_and_rendered_bundle() -> None:
    for document in (_load(POSTGRES_COMPOSE), _rendered_bundle()):
        for service_name in ("postgres", "postgres-bootstrap"):
            image = _service(document, service_name)["image"]
            assert image == POSTGRES_IMAGE
            assert re.fullmatch(r"postgres:16-alpine@sha256:[0-9a-f]{64}", image)


def test_state_initializer_repairs_all_private_state_directories_then_exits() -> None:
    document = _load(WEB_COMPOSE)
    state_init = _service(document, "state-init")
    command = state_init["command"]

    assert state_init["image"] == ELSPETH_IMAGE
    assert state_init["user"] == "1654:1654"
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


def test_web_and_initializer_share_the_container_serving_bind_address() -> None:
    static = _load(WEB_COMPOSE)
    rendered = _rendered_bundle()

    for document in (static, rendered):
        assert _environment(_service(document, "web"))["ELSPETH_WEB__HOST"] == "0.0.0.0"
        assert _environment(_service(document, "web-init"))["ELSPETH_WEB__HOST"] == "0.0.0.0"


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
    assert web_init["depends_on"]["postgres-bootstrap"]["condition"] == "service_completed_successfully"
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
    sensitive_markers = ("PASSWORD", "SECRET", "KEY", "SIGNING", "FINGERPRINT")
    sensitive_assignments = {
        name: value for name, value in assignments.items() if any(marker in name.upper() for marker in sensitive_markers)
    }
    assert {
        "POSTGRES_PASSWORD",
        "ELSPETH_WEB_SECRET_KEY",
        "ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY",
        "AZURE_OPENAI_API_KEY",
        "ELSPETH_FINGERPRINT_KEY",
    } <= sensitive_assignments.keys()
    assert sensitive_assignments
    assert set(sensitive_assignments.values()) == {""}


def test_example_environment_names_the_repository_root_destination() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "cp deploy/compose/.env.example .env" in text
    assert "repository-root .env" in text


def test_supported_three_file_compose_bundle_renders() -> None:
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
        env=COMPOSE_TEST_ENVIRONMENT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
