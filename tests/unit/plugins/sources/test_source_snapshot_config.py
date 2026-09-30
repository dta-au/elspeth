"""Snapshot admission must agree with the runtime's explicit opt-in check."""

from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.csv_source import CSVSource, CSVSourceConfig
from elspeth.plugins.sources.json_source import JSONSource, JSONSourceConfig


@pytest.fixture(params=["csv", "json", "jsonl", "jsonl-auto"])
def source_kind(request: pytest.FixtureRequest) -> str:
    return request.param


def _source_options(source_kind: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "path": f"input.{source_kind.removesuffix('-auto')}",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    if source_kind in {"json", "jsonl"}:
        options["format"] = source_kind
    return options


@pytest.mark.parametrize("invalid_flag", ["true", "false", 0, 1, None])
@pytest.mark.parametrize("admission", ["config", "constructor"])
def test_snapshot_for_resume_rejects_non_boolean_opt_in(source_kind: str, invalid_flag: object, admission: str) -> None:
    options = _source_options(source_kind)
    options["snapshot_for_resume"] = invalid_flag

    with pytest.raises(PluginConfigError, match=r"snapshot_for_resume.*Input should be a valid boolean"):
        if admission == "config":
            config_model = CSVSourceConfig if source_kind == "csv" else JSONSourceConfig
            config_model.from_dict(options)
        else:
            source_cls = CSVSource if source_kind == "csv" else JSONSource
            source_cls(options)


@pytest.mark.parametrize("flag", [True, False])
def test_snapshot_for_resume_boolean_admission_preserves_runtime_opt_in(source_kind: str, flag: bool) -> None:
    options = _source_options(source_kind)
    options["snapshot_for_resume"] = flag
    config_model = CSVSourceConfig if source_kind == "csv" else JSONSourceConfig
    source_cls = CSVSource if source_kind == "csv" else JSONSource

    admitted = config_model.from_dict(options)
    source = source_cls(options)

    assert admitted.snapshot_for_resume is flag
    assert source.config["snapshot_for_resume"] is flag
    assert (source.config["snapshot_for_resume"] is True) == flag


def test_snapshot_for_resume_omission_keeps_snapshot_disabled(source_kind: str) -> None:
    options = _source_options(source_kind)
    config_model = CSVSourceConfig if source_kind == "csv" else JSONSourceConfig
    source_cls = CSVSource if source_kind == "csv" else JSONSource

    admitted = config_model.from_dict(options)
    source = source_cls(options)

    assert admitted.snapshot_for_resume is False
    assert "snapshot_for_resume" not in source.config
