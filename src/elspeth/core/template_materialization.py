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
# is not collector-legal either. `content_file` (added 2026-11-10) is nested-only,
# under the per-row `file_inject` transform's `files`. Swept the live registry at 2026-08-26: 13
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
    # (plugin_name, container_option_key): the key is ALSO resolved inside each
    # named entry of that plugin's container option, e.g. `tables.<name>.reference_file`.
    # Bound to the plugin name so another plugin's same-named option stays opaque data.
    nested_in: tuple[str, str] | None = None
    # Prefix naming the entry in a nested error; defaults to `label`.
    nested_label: str | None = None
    # False for a nested-only key: it has no top-level form, so the top-level
    # materialization and the in-memory rejection scan both leave it alone.
    top_level: bool = True
    # True: a nested file that is a directory or is not UTF-8 text is refused
    # as a TemplateFileError naming the entry and the file. False (the
    # default) leaves the read fault to propagate unchanged, which the
    # reference_join tests pin.
    unreadable_file_is_config_error: bool = False

    def __post_init__(self) -> None:
        if not self.top_level and self.nested_in is None:
            raise ValueError(
                f"File-backed option {self.file_key!r} is unreachable: it has no top-level form (top_level=False) "
                "and declares no nested container (nested_in=None)"
            )


@dataclass(frozen=True, slots=True)
class MaterializedFileOption:
    content: object
    source: str


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
        nested_in=("reference_join", "tables"),
        nested_label="Reference table",
    ),
    # Nested-only: file_inject has no single-file top-level form. Text for the
    # same reason as reference_file — the web surface substitutes a string, and
    # file_inject parses each entry by its declared `format`.
    FileBackedTemplateOption(
        file_key="content_file",
        content_key="content",
        source_key="content_source",
        label="Injected file",
        content_kind="text",
        nested_in=("file_inject", "files"),
        top_level=False,
        unreadable_file_is_config_error=True,
    ),
)
# The keys with a TOP-LEVEL form. A nested-only key is deliberately absent: at
# the top level of an options mapping it is not a file-backed option at all.
FILE_BACKED_TEMPLATE_OPTION_KEYS = frozenset(rule.file_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY if rule.top_level)


def _nested_rules_for(plugin_name: object) -> tuple[tuple[FileBackedTemplateOption, str], ...]:
    """Return each rule nested under this plugin, paired with its container option key."""
    return tuple(
        (rule, rule.nested_in[1])
        for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY
        if rule.nested_in is not None and rule.nested_in[0] == plugin_name
    )


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
        reference_contents: dict[Path, str] = {}
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
                    reference_contents=reference_contents,
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
            if not rule.top_level:
                continue
            materialized = self._materialize_file_option(result, rule, run_mode=run_mode, source_options=source_options)
            if materialized is not None:
                result.pop(rule.file_key)
                result[rule.content_key] = materialized.content
                result[rule.source_key] = materialized.source
        return result

    def _materialize_file_option(
        self,
        options: Mapping[str, Any],
        rule: FileBackedTemplateOption,
        *,
        run_mode: RunMode,
        source_options: object | None,
        reference_contents: dict[Path, str] | None = None,
    ) -> MaterializedFileOption | None:
        if rule.file_key not in options:
            return None
        if rule.content_key in options:
            raise TemplateFileError(f"Cannot specify both '{rule.content_key}' and '{rule.file_key}'")
        file_ref = options[rule.file_key]
        if type(file_ref) is not str:
            raise TemplateFileError(f"'{rule.file_key}' must be a string")
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
            return MaterializedFileOption(content=archived_content, source=file_ref)
        else:
            file_path = _resolve_template_path(file_ref, self._settings_path, rule.label)
            if reference_contents is None:
                live_content = self._load_content(rule, file_path)
            else:
                if file_path not in reference_contents:
                    try:
                        reference_contents[file_path] = file_path.read_text(encoding="utf-8")
                    except IsADirectoryError as exc:
                        if not rule.unreadable_file_is_config_error:
                            raise
                        raise TemplateFileError(f"{rule.label} is a directory, not a file: {file_path}") from exc
                    except UnicodeDecodeError as exc:
                        if not rule.unreadable_file_is_config_error:
                            raise
                        raise TemplateFileError(
                            f"{rule.label} is not UTF-8 text: {file_path} (first invalid byte at offset {exc.start}). "
                            "Save the file as UTF-8; a binary file cannot be bound."
                        ) from exc
                live_content = reference_contents[file_path]
            if run_mode is RunMode.VERIFY and live_content != archived_content:
                raise TemplateFileError(f"{rule.label} differs from source-run materialized content")
            return MaterializedFileOption(content=live_content, source=file_ref)

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
                plugin_name = plugin_config["plugin"] if "plugin" in plugin_config else None
                for rule, container_key in _nested_rules_for(plugin_name):
                    container = options[container_key] if container_key in options else None
                    if type(container) is not dict:
                        continue
                    for entry_name, entry in container.items():
                        if type(entry) is dict and rule.file_key in entry:
                            present.append(f"{container_key}[{entry_name!r}].{rule.file_key}")
                if not present:
                    continue
                raw_name = plugin_config["name"] if "name" in plugin_config else index
                # The remediation list is DERIVED from the registry, not typed out:
                # a hand-written list silently omits every key added after it,
                # which is the failure this check exists to prevent. Only the rules
                # that apply to THIS node are listed: a nested-only content key is
                # not an option any other plugin can inline.
                nested_rules = tuple(rule for rule, _ in _nested_rules_for(plugin_name))
                inline_instead = ", ".join(
                    rule.content_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY if rule.top_level or rule in nested_rules
                )
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
        reference_contents: dict[Path, str],
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
            materialized = self.materialize_options(options, run_mode=run_mode, source_options=source_options)
            plugin_name = plugin["plugin"] if "plugin" in plugin else None
            for rule, container_key in _nested_rules_for(plugin_name):
                # Read from `materialized`, not `options`, so two rules sharing a
                # container compose instead of the second discarding the first.
                raw_container = materialized[container_key] if container_key in materialized else None
                if type(raw_container) is not dict:
                    continue
                source_container = (
                    source_options[container_key] if type(source_options) is dict and container_key in source_options else None
                )
                materialized[container_key] = self._materialize_nested_entries(
                    raw_container,
                    rule,
                    run_mode=run_mode,
                    source_container=source_container,
                    reference_contents=reference_contents,
                )
            plugin["options"] = materialized
        return plugin

    def _materialize_nested_entries(
        self,
        raw_container: dict[Any, Any],
        rule: FileBackedTemplateOption,
        *,
        run_mode: RunMode,
        source_container: object | None,
        reference_contents: dict[Path, str],
    ) -> dict[Any, Any]:
        entry_label = rule.nested_label if rule.nested_label is not None else rule.label
        entries = dict(raw_container)
        for entry_name, entry in entries.items():
            if type(entry) is not dict:
                continue
            entry_result = dict(entry)
            source_entry = source_container[entry_name] if type(source_container) is dict and entry_name in source_container else None
            try:
                materialized_entry = self._materialize_file_option(
                    entry_result,
                    rule,
                    run_mode=run_mode,
                    source_options=source_entry,
                    reference_contents=reference_contents,
                )
            except TemplateFileError as exc:
                raise TemplateFileError(f"{entry_label} {entry_name!r}: {exc}") from exc
            if materialized_entry is not None:
                entry_result.pop(rule.file_key)
                entry_result[rule.content_key] = materialized_entry.content
                entry_result[rule.source_key] = materialized_entry.source
            entries[entry_name] = entry_result
        return entries

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
