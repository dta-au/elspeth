"""Load-time materialization for file-backed plugin template options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from elspeth.contracts.enums import RunMode

PluginCollectionName = Literal["transforms", "aggregations"]
ContentKind = Literal["text", "yaml"]

# DOCUMENTED EXCLUSION: `collectors` is deliberately absent (elspeth-ca79b2c63a).
#
# Not because a collector cannot carry options, but because it cannot carry
# THESE options. NO REGISTERED file-backed key belongs to a batch-aware plugin.
# Three (`template_file`, `lookup_file`, `system_prompt_file`) belong to the LLM
# transform, and `LLMTransform` is not batch-aware — so an `llm` collector is
# rejected before it runs. `reference_file` (added 2026-08-26) is the first
# registered key outside the LLM transform and does not change this: `ReferenceJoin`
# is a per-row enricher that leaves `is_batch_aware` at its default False, so it
# is not collector-legal either. Swept the live registry at 2026-08-26: 13
# batch-aware (collector-legal) transforms exist and NONE declares any registered
# key, so on a legal collector the key is an extra and `extra="forbid"` rejects it
# at config load.
#
# REACTIVATION TRIGGER — this containment is INCIDENTAL, not designed, and has
# an expiry date. Add `collectors` here the moment any batch-aware plugin
# (builtin or third-party) declares a file-backed option field. Today the
# failure is loud and immediate; after that change it becomes a collector
# silently receiving a filename where every other node kind receives content.
PLUGIN_OPTION_COLLECTIONS: tuple[PluginCollectionName, ...] = ("transforms", "aggregations")


class TemplateFileError(Exception):
    """Error loading template, lookup, or system prompt files."""


@dataclass(frozen=True, slots=True)
class FileBackedTemplateOption:
    file_key: str
    content_key: str
    source_key: str
    label: str
    content_kind: ContentKind


FILE_BACKED_TEMPLATE_OPTION_REGISTRY: tuple[FileBackedTemplateOption, ...] = (
    FileBackedTemplateOption(
        file_key="template_file",
        content_key="prompt_template",
        source_key="prompt_template_source",
        label="Template file",
        content_kind="text",
    ),
    FileBackedTemplateOption(
        file_key="lookup_file",
        content_key="lookup",
        source_key="lookup_source",
        label="Lookup file",
        content_kind="yaml",
    ),
    FileBackedTemplateOption(
        file_key="system_prompt_file",
        content_key="system_prompt",
        source_key="system_prompt_source",
        label="System prompt file",
        content_kind="text",
    ),
    # content_kind="text" is load-bearing, not a default. The web surface fills
    # `reference_content` through inline_content blob substitution, which writes
    # a STRING (core/blobs_inline.py), so a "yaml" kind here would hand the same
    # pydantic field a parsed dict on the CLI and a str on the web. Text on both
    # paths; reference_join parses it itself.
    FileBackedTemplateOption(
        file_key="reference_file",
        content_key="reference_content",
        source_key="reference_source",
        label="Reference table file",
        content_kind="text",
    ),
)
FILE_BACKED_TEMPLATE_OPTION_KEYS = frozenset(rule.file_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY)


def _resolve_template_path(file_ref: str, settings_path: Path, label: str) -> Path:
    """Resolve a template/lookup/prompt file path with containment check."""
    config_root = settings_path.parent.resolve()
    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = (config_root / file_path).resolve()
    else:
        file_path = file_path.resolve()

    try:
        file_path.relative_to(config_root)
    except ValueError as exc:
        raise TemplateFileError(
            f"{label} path traversal blocked: {file_ref!r} resolves to {file_path} which is outside config directory {config_root}"
        ) from exc

    if not file_path.exists():
        raise TemplateFileError(f"{label} not found: {file_path}")

    return file_path


class TemplateOptionMaterializer:
    """Materialize file-backed plugin options using a single option registry."""

    def __init__(self, settings_path: Path) -> None:
        self._settings_path = settings_path

    def materialize_config(
        self,
        raw_config: Mapping[str, Any],
        *,
        run_mode: RunMode = RunMode.LIVE,
        source_settings: object | None = None,
    ) -> dict[str, Any]:
        config = dict(raw_config)
        for collection_name in PLUGIN_OPTION_COLLECTIONS:
            if collection_name not in config or type(config[collection_name]) is not list:
                continue
            collection = config[collection_name]
            source_collection = (
                source_settings[collection_name] if type(source_settings) is dict and collection_name in source_settings else None
            )
            config[collection_name] = [
                self._materialize_plugin_config(
                    plugin_config,
                    run_mode=run_mode,
                    source_plugin=(
                        source_collection[index] if type(source_collection) is list and index < len(source_collection) else None
                    ),
                )
                for index, plugin_config in enumerate(collection)
            ]
        return config

    def materialize_options(
        self,
        options: Mapping[str, Any],
        *,
        run_mode: RunMode = RunMode.LIVE,
        source_options: object | None = None,
    ) -> dict[str, Any]:
        result = dict(options)
        for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY:
            if rule.file_key not in result:
                continue
            if rule.content_key in result:
                raise TemplateFileError(f"Cannot specify both '{rule.content_key}' and '{rule.file_key}'")
            file_ref = result.pop(rule.file_key)
            archived_content: Any = None
            if run_mode is not RunMode.LIVE:
                if (
                    type(source_options) is not dict
                    or rule.source_key not in source_options
                    or source_options[rule.source_key] != file_ref
                    or rule.content_key not in source_options
                ):
                    raise TemplateFileError(
                        f"{rule.label} has no matching materialized content in the source run; replay/verify cannot read an unbound file"
                    )
                archived_content = source_options[rule.content_key]
                if rule.content_kind == "text" and type(archived_content) is not str:
                    raise TemplateFileError(f"{rule.label} source-run content is not retained as text")
            if run_mode is RunMode.REPLAY:
                result[rule.content_key] = archived_content
            else:
                file_path = _resolve_template_path(file_ref, self._settings_path, rule.label)
                live_content = self._load_content(rule, file_path)
                if run_mode is RunMode.VERIFY and live_content != archived_content:
                    raise TemplateFileError(f"{rule.label} differs from source-run materialized content")
                result[rule.content_key] = live_content
            result[rule.source_key] = file_ref
        return result

    @staticmethod
    def reject_file_backed_options(raw_config: Mapping[str, object]) -> None:
        for collection_name in PLUGIN_OPTION_COLLECTIONS:
            collection = raw_config[collection_name] if collection_name in raw_config else None
            if type(collection) is not list:
                continue
            for index, plugin_config in enumerate(collection):
                if type(plugin_config) is not dict:
                    continue
                options = plugin_config["options"] if "options" in plugin_config else None
                if type(options) is not dict:
                    continue
                present = sorted(key for key in FILE_BACKED_TEMPLATE_OPTION_KEYS if key in options)
                if not present:
                    continue
                raw_name = plugin_config["name"] if "name" in plugin_config else index
                # The remediation list is DERIVED from the registry, not typed out:
                # a hand-written list silently omits every key added after it,
                # which is the failure this check exists to prevent.
                inline_instead = ", ".join(rule.content_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY)
                raise ValueError(
                    "load_settings_from_yaml_string() cannot expand file-backed template options "
                    f"{present} for {collection_name}[{raw_name!r}] because in-memory web execution "
                    "has no trusted settings file base path. Use load_settings() for file-backed "
                    f"configs, or inline {inline_instead} before web validation/execution."
                )

    def _materialize_plugin_config(
        self,
        plugin_config: Any,
        *,
        run_mode: RunMode,
        source_plugin: Any,
    ) -> Any:
        if type(plugin_config) is not dict:
            return plugin_config
        plugin = dict(plugin_config)
        options = plugin["options"] if "options" in plugin else None
        if type(options) is dict:
            source_options = None
            if (
                type(source_plugin) is dict
                and (source_plugin["plugin"] if "plugin" in source_plugin else None) == (plugin["plugin"] if "plugin" in plugin else None)
                and (source_plugin["name"] if "name" in source_plugin else None) == (plugin["name"] if "name" in plugin else None)
            ):
                candidate = source_plugin["options"] if "options" in source_plugin else None
                if type(candidate) is dict:
                    source_options = candidate
            plugin["options"] = self.materialize_options(options, run_mode=run_mode, source_options=source_options)
        return plugin

    def _load_content(self, rule: FileBackedTemplateOption, file_path: Path) -> Any:
        if rule.content_kind == "text":
            return file_path.read_text(encoding="utf-8")
        try:
            loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            return loaded if loaded is not None else {}
        except yaml.YAMLError as e:
            raise TemplateFileError(f"Invalid YAML in lookup file: {e}") from e


def _expand_template_files(options: dict[str, Any], settings_path: Path) -> dict[str, Any]:
    return TemplateOptionMaterializer(settings_path).materialize_options(options)
