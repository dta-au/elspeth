"""Chroma's absent client-mode directory survives every path admission stage."""

from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.sinks.chroma_sink import ChromaSinkConfig
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.tools._common import _validate_sink_path
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.validation import validate_pipeline
from tests.unit.web.composer.test_tools import execute_tool
from tests.unit.web.execution.test_local_database_sink_quota_admission import _chroma_state
from tests.unit.web.execution.test_preflight_side_effects import _web_settings
from tests.unit.web.plugin_policy.test_availability import _build_with_policy


@pytest.mark.parametrize("entry", ["admission", "saved_preview"])
def test_chroma_client_explicit_null_directory_is_admitted(tmp_path: Path, entry: str) -> None:
    state = _chroma_state(tmp_path, persistent=False)
    output = state.outputs[0]
    options = {**deep_thaw(output.options), "persist_directory": None}
    config = ChromaSinkConfig.model_validate(options)
    assert config.mode == "client"
    assert config.persist_directory is None
    catalog = create_catalog_service()
    if entry == "admission":
        result = execute_tool(
            "set_output",
            {"sink_name": output.name, "plugin": output.plugin, "options": options, "on_write_failure": "discard"},
            state,
            catalog,
            data_dir=str(tmp_path),
        )
        assert result.success, result
        assert result.updated_state.outputs[0].options["persist_directory"] is None
    else:
        state = replace(state, outputs=(replace(output, options=options),))
        settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:chroma_sink",)})
        _policy, snapshot = _build_with_policy(settings)
        validation = validate_pipeline(
            state, settings, yaml_generator, plugin_snapshot=snapshot, profile_registry=None, catalog=catalog, session_id="test-session"
        )
        assert validation.is_valid, validation.errors
        result = execute_tool("preview_pipeline", {}, state, catalog, data_dir=str(tmp_path), runtime_preflight=lambda _: validation)
        assert result.success, result
        assert result.data["preview_is_valid"] is True


@pytest.mark.parametrize(
    ("plugin", "options"),
    [
        ("csv", {"mode": "client", "persist_directory": None}),
        ("chroma_sink", {"mode": "persistent", "persist_directory": None}),
        ("chroma_sink", {"mode": "client", "path": None}),
        ("chroma_sink", {"mode": "client", "persist_directory": "/outside/index"}),
    ],
)
def test_chroma_null_exception_cannot_bypass_required_paths(tmp_path: Path, plugin: str, options: dict[str, object]) -> None:
    assert _validate_sink_path(options, str(tmp_path), plugin=plugin, session_id="test-session") is not None
