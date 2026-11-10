"""file_inject binds `files.<name>.content_file` through the file-option registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.enums import RunMode
from elspeth.core.template_materialization import (
    FILE_BACKED_TEMPLATE_OPTION_KEYS,
    FILE_BACKED_TEMPLATE_OPTION_REGISTRY,
    FileBackedTemplateOption,
    TemplateFileError,
    TemplateOptionMaterializer,
)


def _config(options: object, *, plugin: str = "file_inject") -> dict[str, object]:
    return {"transforms": [{"name": "bind", "plugin": plugin, "options": options}]}


def _options(config: dict) -> dict:
    return config["transforms"][0]["options"]


def test_content_file_becomes_content_and_source(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    raw = _config({"files": {"A": {"content_file": "a.md", "format": "text"}}})

    result = TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(raw)

    assert _options(result) == {"files": {"A": {"content": "hello\n", "content_source": "a.md", "format": "text"}}}
    assert _options(raw) == {"files": {"A": {"content_file": "a.md", "format": "text"}}}


def test_content_file_outside_settings_dir_is_refused(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / "outside.md").write_text("secret\n", encoding="utf-8")

    with pytest.raises(TemplateFileError) as exc:
        TemplateOptionMaterializer(config_dir / "settings.yaml").materialize_config(
            _config({"files": {"A": {"content_file": "../outside.md"}}})
        )

    assert str(exc.value).startswith("Injected file 'A': ")
    assert "path traversal blocked" in str(exc.value)


def test_missing_content_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TemplateFileError) as exc:
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(_config({"files": {"A": {"content_file": "missing.md"}}}))

    assert str(exc.value).startswith("Injected file 'A': Injected file not found: ")


def test_content_and_content_file_together_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")

    with pytest.raises(TemplateFileError) as exc:
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(
            _config({"files": {"A": {"content_file": "a.md", "content": "inline"}}})
        )

    assert str(exc.value) == "Injected file 'A': Cannot specify both 'content' and 'content_file'"


@pytest.mark.parametrize("plugin", ["file_inject", "llm"])
def test_top_level_content_file_is_not_materialized(tmp_path: Path, plugin: str) -> None:
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    raw = _config({"content_file": "a.md"}, plugin=plugin)
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")

    assert materializer.materialize_config(raw) == raw
    assert materializer.materialize_options({"content_file": "a.md"}) == {"content_file": "a.md"}


@pytest.mark.parametrize("plugin", ["file_inject", "llm"])
def test_in_memory_loading_ignores_top_level_content_file(plugin: str) -> None:
    """`content_file` is nested-only, so the top-level scan must not claim it."""
    TemplateOptionMaterializer.reject_file_backed_options(_config({"content_file": "a.md"}, plugin=plugin))

    assert "content_file" not in FILE_BACKED_TEMPLATE_OPTION_KEYS
    assert "content_file" in {rule.file_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY}


def test_replay_uses_recorded_content_without_reading(tmp_path: Path) -> None:
    injected = tmp_path / "a.md"
    injected.write_text("changed since the run\n", encoding="utf-8")
    injected.unlink()
    raw = _config({"files": {"A": {"content_file": "a.md", "format": "text"}}})
    source = _config({"files": {"A": {"content": "recorded\n", "content_source": "a.md", "format": "text"}}})
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")

    result = materializer.materialize_config(raw, run_mode=RunMode.REPLAY, source_settings=source)

    assert _options(result) == _options(source)
    for unbound in [None, _config({"files": {"B": _options(source)["files"]["A"]}}), _config({"files": {"A": {"content_source": "b.md"}}})]:
        with pytest.raises(TemplateFileError, match=r"^Injected file 'A': Injected file has no matching materialized content"):
            materializer.materialize_config(raw, run_mode=RunMode.REPLAY, source_settings=unbound)


def test_verify_rejects_content_that_drifted_from_the_source_run(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("current\n", encoding="utf-8")
    raw = _config({"files": {"A": {"content_file": "a.md"}}})
    source = _config({"files": {"A": {"content": "recorded\n", "content_source": "a.md"}}})

    with pytest.raises(TemplateFileError, match=r"^Injected file 'A': Injected file differs from source-run"):
        TemplateOptionMaterializer(tmp_path / "settings.yaml").materialize_config(raw, run_mode=RunMode.VERIFY, source_settings=source)


def test_in_memory_loading_rejects_nested_content_file() -> None:
    with pytest.raises(ValueError) as exc:
        TemplateOptionMaterializer.reject_file_backed_options(_config({"files": {"A": {"content_file": "a.md"}}}))

    message = str(exc.value)
    assert "files['A'].content_file" in message
    assert "transforms['bind']" in message


def test_other_plugins_files_option_is_untouched(tmp_path: Path) -> None:
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    for plugin in ("llm", "reference_join"):
        unrelated = _config({"files": {"A": {"content_file": "missing.md"}}}, plugin=plugin)
        assert materializer.materialize_config(unrelated) == unrelated
        TemplateOptionMaterializer.reject_file_backed_options(unrelated)


def test_file_inject_does_not_resolve_another_plugins_nested_key(tmp_path: Path) -> None:
    """Each nested key is bound to its own (plugin, container) pair."""
    materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
    for options in ({"tables": {"A": {"content_file": "missing.md"}}}, {"files": {"A": {"reference_file": "missing.csv"}}}):
        crossed = _config(options)
        assert materializer.materialize_config(crossed) == crossed
        TemplateOptionMaterializer.reject_file_backed_options(crossed)


_BASE_REMEDIATION_TAIL = (
    "has no trusted settings file base path. Use load_settings() for file-backed "
    "configs, or inline prompt_template, lookup, system_prompt, reference_content before web validation/execution."
)


@pytest.mark.parametrize(
    ("plugin", "options", "present"),
    [
        ("reference_join", {"tables": {"products": {"reference_file": "p.csv"}}}, "[\"tables['products'].reference_file\"]"),
        ("reference_join", {"reference_file": "p.csv"}, "['reference_file']"),
        ("llm", {"template_file": "t.j2"}, "['template_file']"),
    ],
)
def test_in_memory_rejection_text_is_unchanged_for_existing_plugins(plugin: str, options: dict, present: str) -> None:
    """Byte-for-byte the message on base commit 84d07a8d: no nested-only `content`."""
    with pytest.raises(ValueError) as exc:
        TemplateOptionMaterializer.reject_file_backed_options(_config(options, plugin=plugin))

    assert str(exc.value) == (
        "load_settings_from_yaml_string() cannot expand file-backed template options "
        f"{present} for transforms['bind'] because in-memory web execution " + _BASE_REMEDIATION_TAIL
    )


def test_in_memory_rejection_tells_file_inject_to_inline_content() -> None:
    with pytest.raises(ValueError) as exc:
        TemplateOptionMaterializer.reject_file_backed_options(_config({"files": {"A": {"content_file": "a.md"}}}))

    assert str(exc.value) == (
        "load_settings_from_yaml_string() cannot expand file-backed template options "
        "[\"files['A'].content_file\"] for transforms['bind'] because in-memory web execution "
        "has no trusted settings file base path. Use load_settings() for file-backed "
        "configs, or inline prompt_template, lookup, system_prompt, reference_content, content before web validation/execution."
    )


def test_a_rule_with_neither_a_top_level_nor_a_nested_form_is_refused() -> None:
    with pytest.raises(ValueError, match=r"'orphan_file'.*unreachable"):
        FileBackedTemplateOption(
            file_key="orphan_file",
            content_key="orphan",
            source_key="orphan_source",
            label="Orphan file",
            content_kind="text",
            top_level=False,
        )
