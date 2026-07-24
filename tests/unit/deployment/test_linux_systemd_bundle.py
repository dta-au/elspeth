"""Static contract tests for the portable native Linux systemd bundle."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE = REPO_ROOT / "deploy" / "linux-systemd" / "elspeth-web.service"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "linux-systemd" / "elspeth-web.env.example"


def _service_text() -> str:
    return SERVICE.read_text(encoding="utf-8")


def _active_service_lines() -> list[str]:
    return [line.strip() for line in _service_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _active_example_assignments() -> dict[str, str]:
    return {
        key: value
        for raw_line in _example_text().splitlines()
        if (line := raw_line.strip()) and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }


def test_portable_service_uses_dedicated_identity_and_install_locations() -> None:
    lines = _active_service_lines()

    assert "User=elspeth" in lines
    assert "Group=elspeth" in lines
    assert "WorkingDirectory=/opt/elspeth" in lines
    assert "EnvironmentFile=/etc/elspeth/elspeth-web.env" in lines
    assert "/home/john" not in _service_text()


def test_portable_service_creates_private_persistent_state() -> None:
    lines = _active_service_lines()

    assert "StateDirectory=elspeth" in lines
    assert "StateDirectoryMode=0700" in lines
    assert ("ExecStartPre=/usr/bin/install -d -m 0700 /var/lib/elspeth/data/blobs /var/lib/elspeth/payloads") in lines
    assert "ReadWritePaths=/var/lib/elspeth /run/elspeth" in lines


def test_portable_service_runs_exactly_one_web_process() -> None:
    lines = _active_service_lines()

    assert "Environment=ELSPETH_WEB__DEPLOYMENT_TARGET=linux-systemd" in lines
    assert "Environment=WEB_CONCURRENCY=1" in lines
    assert ("ExecStart=/usr/bin/env /opt/elspeth/.venv/bin/elspeth web --host 0.0.0.0 --port 8451") in lines
    assert not any("--workers" in line for line in lines)


def test_portable_service_retains_restart_and_hardening_contract() -> None:
    lines = _active_service_lines()

    assert "Restart=on-failure" in lines
    expected_hardening = {
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    }
    assert expected_hardening <= set(lines)


def test_linux_environment_example_defaults_to_private_single_host_sqlite() -> None:
    assignments = _active_example_assignments()

    assert assignments["ELSPETH_WEB__DEPLOYMENT_TARGET"] == "linux-systemd"
    assert assignments["ELSPETH_WEB__DEPLOYMENT_STATE_MODE"] == "sqlite-single"
    assert assignments["ELSPETH_WEB__DATA_DIR"] == "/var/lib/elspeth/data"
    assert assignments["ELSPETH_WEB__PAYLOAD_STORE_PATH"] == "/var/lib/elspeth/payloads"
    assert not any(name.endswith("_URL") and not value for name, value in assignments.items())


def test_linux_environment_example_documents_distinct_external_databases() -> None:
    text = _example_text()

    session_example = re.search(r"^#\s*ELSPETH_WEB__SESSION_DB_URL=(\S+)$", text, re.MULTILINE)
    landscape_example = re.search(r"^#\s*ELSPETH_WEB__LANDSCAPE_URL=(\S+)$", text, re.MULTILINE)
    assert session_example is not None
    assert landscape_example is not None
    assert session_example.group(1).startswith("postgresql+psycopg://")
    assert landscape_example.group(1).startswith("postgresql+psycopg://")
    assert session_example.group(1) != landscape_example.group(1)
    assert "external-postgresql" in text
    assert "two" in text.lower()


def test_linux_environment_example_sets_production_composer_limits() -> None:
    assignments = _active_example_assignments()

    assert assignments["ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS"] == "15"
    assert assignments["ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS"] == "10"
    assert assignments["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"] == "85"
    assert assignments["ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE"] == "10"


def test_linux_environment_example_names_secrets_without_shipping_them() -> None:
    assignments = _active_example_assignments()
    required_secret_names = {
        "ELSPETH_WEB__SECRET_KEY",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "AZURE_CONTENT_SAFETY_KEY",
        "AZURE_OPENAI_API_KEY",
        "ELSPETH_FINGERPRINT_KEY",
    }

    assert required_secret_names <= assignments.keys()
    assert {assignments[name] for name in required_secret_names} == {""}
