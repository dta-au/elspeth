"""Static policy assertions on ``gateway/Dockerfile`` text, plus unit tests
for ``core.app.build()`` -- the ``uvicorn --factory`` entry point that
Dockerfile's ``ENTRYPOINT`` names.

The Dockerfile assertions here never invoke a docker daemon: they parse the
Dockerfile as text, so they run in the same fast, hermetic ``pytest tests``
pass as everything else. The real proof that the image builds, runs
non-root, serves traffic, and survives a read-only rootfs is the separate,
manual container-verification step recorded in the task report -- that step
needs a docker daemon this suite cannot assume.
"""

import re
from pathlib import Path

import pytest
from elspeth_llm_gateway.core.app import build
from elspeth_llm_gateway.core.config import ConfigError

_DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"

BEARER = "b" * 40
CLIENT_SECRET = "c" * 40

VALID_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": BEARER,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": "https://upstream.example.com",
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": "https://auth.example.com/token",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": "client-id-value",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
    "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
    "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
    "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
    "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": '{"gpt-4o": {"target": "backend-a"}}',
}


def _dockerfile_text() -> str:
    return _DOCKERFILE_PATH.read_text()


def _final_stage_text() -> str:
    """The text of the last ``FROM`` stage only -- where the builder's
    ``pip install`` (a required, legitimate build step) must not be
    mistaken for a final-stage install."""
    stages = re.split(r"(?m)^FROM ", _dockerfile_text())
    return stages[-1]


# --- structural presence -----------------------------------------------------


def test_dockerfile_exists():
    assert _DOCKERFILE_PATH.is_file()


def test_dockerfile_runs_as_nonroot_uid_65532():
    assert re.search(r"(?m)^USER\s+65532(:65532)?\s*$", _dockerfile_text())


def test_dockerfile_pins_base_image_by_digest_not_floating_tag():
    text = _dockerfile_text()
    from_lines = [line for line in text.splitlines() if line.strip().upper().startswith("FROM ")]
    assert from_lines, "Dockerfile has no FROM line"
    for line in from_lines:
        assert "@sha256:" in line, f"FROM line not digest-pinned: {line!r}"
        assert "latest" not in line.lower(), f"FROM line uses a floating tag: {line!r}"


def test_dockerfile_final_stage_has_no_pip_install():
    assert "pip install" not in _final_stage_text()


def test_dockerfile_has_no_add_instruction():
    assert not re.search(r"(?m)^\s*ADD\s", _dockerfile_text())


def test_dockerfile_sets_pythondontwritebytecode_and_venv_path():
    text = _dockerfile_text()
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "/venv/bin:$PATH" in text


def test_dockerfile_exposes_8787():
    assert re.search(r"(?m)^EXPOSE\s+8787\s*$", _dockerfile_text())


def test_dockerfile_entrypoint_uses_absolute_venv_python():
    entrypoint_match = re.search(r"(?m)^ENTRYPOINT\s+(\[.*\])\s*$", _dockerfile_text())
    assert entrypoint_match, "no ENTRYPOINT instruction found"
    assert entrypoint_match.group(1).startswith('["/venv/bin/python"'), (
        "ENTRYPOINT must invoke the absolute venv interpreter, not a bare 'python'"
    )


def test_dockerfile_entrypoint_includes_factory_and_graceful_shutdown_flags():
    entrypoint_match = re.search(r"(?m)^ENTRYPOINT\s+(\[.*\])\s*$", _dockerfile_text())
    assert entrypoint_match
    entrypoint_line = entrypoint_match.group(1)
    assert '"--factory"' in entrypoint_line
    assert '"--timeout-graceful-shutdown"' in entrypoint_line
    assert '"30"' in entrypoint_line
    assert "elspeth_llm_gateway.core.app:build" in entrypoint_line


def test_dockerfile_entrypoint_is_exec_form_not_shell():
    entrypoint_match = re.search(r"(?m)^ENTRYPOINT\s+(.*)$", _dockerfile_text())
    assert entrypoint_match
    assert entrypoint_match.group(1).strip().startswith("["), "ENTRYPOINT must use exec (JSON array) form, not shell form"


# --- build() factory ---------------------------------------------------------


def test_build_returns_app_from_valid_environment(monkeypatch):
    monkeypatch.setattr("os.environ", dict(VALID_ENV))
    app = build()
    assert app.title == "FastAPI"  # default title: just confirms a real FastAPI instance came back
    assert any(route.path == "/healthz" for route in app.routes)


def test_build_raises_config_error_on_missing_required_env(monkeypatch):
    incomplete_env = dict(VALID_ENV)
    del incomplete_env["ELSPETH_LLM_GATEWAY_INBOUND_BEARER"]
    monkeypatch.setattr("os.environ", incomplete_env)

    with pytest.raises(ConfigError) as excinfo:
        build()

    assert "missing_env:ELSPETH_LLM_GATEWAY_INBOUND_BEARER" in excinfo.value.errors


def test_build_raises_config_error_on_unknown_env_var(monkeypatch):
    polluted_env = dict(VALID_ENV)
    polluted_env["ELSPETH_LLM_GATEWAY_NOT_A_REAL_SETTING"] = "whatever"
    monkeypatch.setattr("os.environ", polluted_env)

    with pytest.raises(ConfigError) as excinfo:
        build()

    assert "unknown_env:ELSPETH_LLM_GATEWAY_NOT_A_REAL_SETTING" in excinfo.value.errors


def test_build_config_error_codes_never_echo_secret_values(monkeypatch):
    """Every code in a ConfigError is a safe, closed-vocabulary string --
    never the raw secret value that failed validation."""
    bad_env = dict(VALID_ENV)
    bad_env["ELSPETH_LLM_GATEWAY_INBOUND_BEARER"] = "too-short"
    monkeypatch.setattr("os.environ", bad_env)

    with pytest.raises(ConfigError) as excinfo:
        build()

    for code in excinfo.value.errors:
        assert "too-short" not in code
        assert code.replace(":", "").replace("_", "").isalnum() or code.startswith("invalid_model_alias:")
