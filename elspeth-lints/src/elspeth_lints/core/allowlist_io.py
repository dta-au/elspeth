"""Shared YAML IO helpers for allowlist CI gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from elspeth_lints.core.allowlist import AllowlistEntry, _parse_allow_hits

_HISTORICAL_BASELINE_SAFETY = "historical-baseline-unspecified"


class AllowlistIOError(RuntimeError):
    """An allowlist YAML document could not be read or parsed."""


@dataclass(frozen=True, slots=True)
class AllowlistYamlDocument:
    """One parsed allowlist YAML file."""

    source_file: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PerFileRuleRecord:
    """Rule-agnostic parsed representation shared by CI policy gates."""

    source_file: str
    index: int
    pattern: str
    rules: tuple[str, ...]
    reason: str
    expires: str | None
    max_hits: int | None

    @property
    def label(self) -> str:
        return f"per_file_rules[{self.index}]::pattern={self.pattern}::rules={','.join(self.rules)}"


def load_yaml_mapping_text(text: str, *, source_label: str) -> dict[str, Any]:
    """Parse YAML text as a mapping or raise ``AllowlistIOError``."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise AllowlistIOError(f"{source_label}: failed to parse as YAML mapping: {exc}") from exc
    if not isinstance(raw, dict):
        raise AllowlistIOError(f"{source_label}: failed to parse as YAML mapping: YAML root must be a mapping, got {type(raw).__name__}")
    return raw


def iter_yaml_documents(directory: Path) -> list[AllowlistYamlDocument]:
    """Return parsed non-default YAML files in ``directory``."""
    documents: list[AllowlistYamlDocument] = []
    for yaml_file in sorted(directory.glob("*.yaml")):
        if yaml_file.name == "_defaults.yaml":
            continue
        try:
            text = yaml_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise AllowlistIOError(f"could not read {yaml_file}: {exc}") from exc
        documents.append(
            AllowlistYamlDocument(
                source_file=yaml_file.name,
                data=load_yaml_mapping_text(text, source_label=str(yaml_file)),
            )
        )
    return documents


def parse_allow_hits(
    data: dict[str, Any],
    *,
    source_file: str,
    allow_historical_missing_safety: bool = False,
) -> list[AllowlistEntry]:
    """Parse ``allow_hits`` entries from one YAML mapping."""
    parse_data = _with_historical_baseline_safety(data) if allow_historical_missing_safety else data
    try:
        return _parse_allow_hits(parse_data, source_file=source_file, source_root=None)
    except (ValueError, TypeError) as exc:
        raise AllowlistIOError(f"{source_file}: allow_hits entry shape violated loader invariants: {exc}") from exc


def parse_per_file_rules(data: dict[str, Any], *, source_file: str) -> list[PerFileRuleRecord]:
    """Parse ``per_file_rules`` without coupling to one rule vocabulary."""
    raw_entries = data.get("per_file_rules", [])
    if raw_entries is None:
        return []
    if not isinstance(raw_entries, list):
        raise AllowlistIOError(f"{source_file}: per_file_rules must be a list if present")

    entries: list[PerFileRuleRecord] = []
    for index, raw_entry in enumerate(raw_entries):
        context = f"per_file_rules[{index}]"
        if not isinstance(raw_entry, dict):
            raise AllowlistIOError(f"{source_file}: {context} must be a mapping")
        pattern = _required_string(raw_entry, "pattern", context=context, source_file=source_file)
        rules = tuple(_required_string_list(raw_entry, "rules", context=context, source_file=source_file))
        if not rules:
            raise AllowlistIOError(f"{source_file}: {context}.rules must be a non-empty list")
        if len(set(rules)) != len(rules):
            raise AllowlistIOError(f"{source_file}: {context}.rules must not contain duplicate rule ids")
        entries.append(
            PerFileRuleRecord(
                source_file=source_file,
                index=index,
                pattern=pattern,
                rules=rules,
                reason=_required_string(raw_entry, "reason", context=context, source_file=source_file),
                expires=_optional_date(raw_entry, "expires", context=context, source_file=source_file),
                max_hits=_optional_int(raw_entry, "max_hits", context=context, source_file=source_file),
            )
        )
    return entries


def _required_string(data: dict[str, Any], key: str, *, context: str, source_file: str) -> str:
    if key not in data:
        raise AllowlistIOError(f"{source_file}: {context} must include {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise AllowlistIOError(f"{source_file}: {context}.{key} must be a non-empty string")
    return value


def _required_string_list(data: dict[str, Any], key: str, *, context: str, source_file: str) -> list[str]:
    raw_values = data.get(key)
    if not isinstance(raw_values, list):
        raise AllowlistIOError(f"{source_file}: {context}.{key} must be a list")
    values: list[str] = []
    for index, value in enumerate(raw_values):
        if not isinstance(value, str) or not value:
            raise AllowlistIOError(f"{source_file}: {context}.{key}[{index}] must be a non-empty string")
        values.append(value)
    return values


def _optional_date(data: dict[str, Any], key: str, *, context: str, source_file: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, datetime):
        raise AllowlistIOError(f"{source_file}: {context}.{key} must be YYYY-MM-DD, null, or absent")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise AllowlistIOError(f"{source_file}: {context}.{key} must be YYYY-MM-DD, null, or absent") from exc
        return value
    raise AllowlistIOError(f"{source_file}: {context}.{key} must be YYYY-MM-DD, null, or absent")


def _optional_int(data: dict[str, Any], key: str, *, context: str, source_file: str) -> int | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllowlistIOError(f"{source_file}: {context}.{key} must be an integer, null, or absent")
    return int(value)


def _with_historical_baseline_safety(data: dict[str, Any]) -> dict[str, Any]:
    """Fill the post-hoc safety field for read-only historical baseline entries."""
    raw_entries = data.get("allow_hits")
    if not isinstance(raw_entries, list):
        return data

    entries: list[Any] = []
    changed = False
    for raw_entry in raw_entries:
        if isinstance(raw_entry, dict) and "safety" not in raw_entry:
            entries.append({**raw_entry, "safety": _HISTORICAL_BASELINE_SAFETY})
            changed = True
        else:
            entries.append(raw_entry)
    if not changed:
        return data
    return {**data, "allow_hits": entries}


def iter_allow_hits_from_directory(directory: Path) -> list[AllowlistEntry]:
    """Return every ``allow_hits`` entry in a directory of allowlist YAML files."""
    entries: list[AllowlistEntry] = []
    for document in iter_yaml_documents(directory):
        if "allow_hits" not in document.data:
            continue
        entries.extend(parse_allow_hits(document.data, source_file=document.source_file))
    return entries


def entry_shape_count(data: dict[str, Any], key: str, *, source_file: str) -> int:
    """Return list length for an allowlist entry-shape key."""
    raw_entries = data.get(key, [])
    if raw_entries is None:
        return 0
    if not isinstance(raw_entries, list):
        raise AllowlistIOError(f"{source_file}: {key} must be a list if present")
    return len(raw_entries)
