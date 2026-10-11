"""Named reference tables use the same bound file snapshots as legacy options."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.contracts.enums import RunMode
from elspeth.core.template_materialization import TemplateFileError, TemplateOptionMaterializer


def _config(tables: object, *, plugin: str = "reference_join") -> dict[str, object]:
    return {"transforms": [{"name": "enrich", "plugin": plugin, "options": {"tables": tables}}]}


def _tables(config: dict) -> dict:
    return config["transforms"][0]["options"]["tables"]


def test_nested_tables_materialize_once_per_resolved_file_and_keep_source_identity(tmp_path: Path) -> None:
    reference = tmp_path / "products.csv"
    reference.write_text("sku,name\nA,Alpha\n", encoding="utf-8")
    raw = _config({"products": {"reference_file": "products.csv"}, "other": {"reference_file": "./products.csv"}})
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    original_read = Path.read_text
    reads = []

    def read(path: Path, *args: object, **kwargs: object) -> str:
        reads.append(path)
        return original_read(path, *args, **kwargs)

    with patch.object(Path, "read_text", read):
        result = materializer.materialize_config(raw)

    assert reads == [reference]
    assert _tables(result) == {
        "products": {"reference_content": "sku,name\nA,Alpha\n", "reference_source": "products.csv"},
        "other": {"reference_content": "sku,name\nA,Alpha\n", "reference_source": "./products.csv"},
    }
    assert "reference_file" in _tables(raw)["products"]
    reference.write_text("sku,name\nA,Changed\n", encoding="utf-8")
    assert _tables(materializer.materialize_config(raw))["products"]["reference_content"] == "sku,name\nA,Changed\n"


@pytest.mark.parametrize("run_mode", [RunMode.REPLAY, RunMode.VERIFY])
def test_nested_tables_bind_archive_by_name_and_file_identity(tmp_path: Path, run_mode: RunMode) -> None:
    reference = tmp_path / "products.csv"
    reference.write_text("sku,name\nA,Alpha\n", encoding="utf-8")
    raw = _config({"products": {"reference_file": "products.csv"}})
    source = _config({"products": {"reference_source": "products.csv", "reference_content": "sku,name\nA,Alpha\n"}})
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    if run_mode is RunMode.REPLAY:
        reference.unlink()

    assert _tables(materializer.materialize_config(raw, run_mode=run_mode, source_settings=source)) == _tables(source)
    for unbound in [None, _config({"renamed": _tables(source)["products"]}), _config({"products": {"reference_source": "other.csv"}})]:
        with pytest.raises(TemplateFileError, match="no matching materialized content"):
            materializer.materialize_config(raw, run_mode=run_mode, source_settings=unbound)


def test_nested_verify_rejects_drift_and_nontext_archive(tmp_path: Path) -> None:
    (tmp_path / "products.csv").write_text("current", encoding="utf-8")
    raw = _config({"products": {"reference_file": "products.csv"}})
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    for archived, message in [("archived", "differs from source-run"), ({}, "not retained as text")]:
        source = _config({"products": {"reference_source": "products.csv", "reference_content": archived}})
        with pytest.raises(TemplateFileError, match=message):
            materializer.materialize_config(raw, run_mode=RunMode.VERIFY, source_settings=source)


@pytest.mark.parametrize("file_ref", ["../outside.csv", "missing.csv"])
def test_nested_tables_enforce_containment_and_missing_file_errors(tmp_path: Path, file_ref: str) -> None:
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    with pytest.raises(TemplateFileError, match=r"path traversal blocked|not found"):
        materializer.materialize_config(_config({"products": {"reference_file": file_ref}}))


def test_nested_table_rejects_file_and_inline_content_together(tmp_path: Path) -> None:
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    with pytest.raises(TemplateFileError, match="Cannot specify both"):
        materializer.materialize_config(_config({"products": {"reference_file": "products.csv", "reference_content": "inline"}}))


def test_web_rejects_nested_reference_files_with_table_location() -> None:
    with pytest.raises(ValueError, match=r"tables.*products.*reference_file"):
        TemplateOptionMaterializer.reject_file_backed_options(_config({"products": {"reference_file": "products.csv"}}))


def test_materialization_does_not_walk_unrelated_nested_option_data(tmp_path: Path) -> None:
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    unrelated = _config({"products": {"reference_file": "missing.csv"}}, plugin="llm")
    assert materializer.materialize_config(unrelated) == unrelated
    TemplateOptionMaterializer.reject_file_backed_options(unrelated)
    inline = _config({"products": {"reference_content": "csv", "lookup_file": "uninterpreted.yaml"}})
    assert materializer.materialize_config(inline) == inline
    TemplateOptionMaterializer.reject_file_backed_options(inline)


def test_nested_table_symlink_escape_is_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("sku,name\nA,Alpha\n", encoding="utf-8")
    (config_dir / "products.csv").symlink_to(outside)

    with pytest.raises(TemplateFileError, match="path traversal blocked"):
        TemplateOptionMaterializer(config_dir / "settings.yaml").materialize_config(
            _config({"products": {"reference_file": "products.csv"}})
        )


def test_cached_verify_still_compares_each_tables_archived_content(tmp_path: Path) -> None:
    (tmp_path / "products.csv").write_text("current", encoding="utf-8")
    raw = _config({"first": {"reference_file": "products.csv"}, "second": {"reference_file": "products.csv"}})
    source = _config(
        {
            "first": {"reference_source": "products.csv", "reference_content": "current"},
            "second": {"reference_source": "products.csv", "reference_content": "different archive"},
        }
    )

    with pytest.raises(TemplateFileError, match=r"second.*differs from source-run"):
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(raw, run_mode=RunMode.VERIFY, source_settings=source)


def test_nested_table_read_error_propagates_as_infrastructure_fault(tmp_path: Path) -> None:
    (tmp_path / "products.csv").mkdir()

    with pytest.raises(IsADirectoryError):
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(_config({"products": {"reference_file": "products.csv"}}))


@pytest.mark.parametrize("file_ref", [None, 42, True, ["products.csv"]])
@pytest.mark.parametrize("run_mode", [RunMode.LIVE, RunMode.REPLAY])
def test_nested_file_reference_requires_a_string(tmp_path: Path, file_ref: object, run_mode: RunMode) -> None:
    with pytest.raises(TemplateFileError, match=r"reference_file.*must be a string"):
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(
            _config({"products": {"reference_file": file_ref}}), run_mode=run_mode
        )
