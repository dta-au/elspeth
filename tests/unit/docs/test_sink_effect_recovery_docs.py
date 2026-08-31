"""Contracts between live sink-effect settings and their operator documentation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from elspeth.contracts import SinkEffectReconcileKind
from elspeth.core.config import LandscapeExportSettings
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH

ROOT = Path(__file__).parents[3]
RUNBOOK = ROOT / "docs/runbooks/sink-effect-recovery.md"
CONFIGURATION = ROOT / "docs/reference/configuration.md"


def _runbook_applicability_epoch(content: str) -> int:
    matches = re.findall(
        r"\bapplies to Landscape schema epoch (\d+) and the `sink-effect-v1` protocol\.",
        content,
    )
    assert len(matches) == 1, "runbook must contain exactly one current applicability epoch"
    return int(matches[0])


def _configuration_current_landscape_epoch(content: str) -> int:
    matches = re.findall(r"^### Landscape schema epoch (\d+)$", content, flags=re.MULTILINE)
    assert len(matches) == 1, "configuration must contain exactly one current Landscape epoch heading"
    return int(matches[0])


def _export_setting_rows() -> dict[str, tuple[str, str]]:
    content = CONFIGURATION.read_text(encoding="utf-8")
    section = content.split("### Export Settings", maxsplit=1)[1].split(
        "### Sink-effect resource and transport bounds",
        maxsplit=1,
    )[0]
    rows: dict[str, tuple[str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        field, _type, default, description = (cell.strip() for cell in line.strip("|").split("|"))
        rows[field.strip("`")] = (default, description)
    return rows


def _reconciliation_decision_rows() -> list[tuple[str, str]]:
    content = RUNBOOK.read_text(encoding="utf-8")
    section = content.split("## Recovery decision", maxsplit=1)[1].split("## Target-specific checks", maxsplit=1)[0]
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not line.startswith("| Reconcile says "):
            continue
        condition, action = (cell.strip() for cell in line.strip("|").split("|"))
        match = re.fullmatch(r"Reconcile says `([A-Z_]+)`", condition)
        assert match is not None
        rows.append((match.group(1), action))
    return rows


def _safe_action_categories(action: str) -> frozenset[str]:
    categories: set[str] = set()
    normalized = action.casefold()
    if re.search(r"\bmay\b.*\bone\b.*\bcommit\b", normalized):
        categories.add("commit_once")
    if re.search(r"\bfinalize\b.*\bdo not commit\b", normalized):
        categories.add("finalize_without_commit")
    if re.search(r"\bblock\w*\b.*\bdo not commit\b", normalized):
        categories.add("block_without_commit")
    return frozenset(categories)


def test_runbook_uses_current_landscape_schema_epoch() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")
    assert _runbook_applicability_epoch(content) == SQLITE_SCHEMA_EPOCH


def test_configuration_uses_current_landscape_schema_epoch() -> None:
    content = CONFIGURATION.read_text(encoding="utf-8")
    assert _configuration_current_landscape_epoch(content) == SQLITE_SCHEMA_EPOCH


def test_configuration_documents_live_enabled_export_requirements() -> None:
    with pytest.raises(ValidationError) as error:
        LandscapeExportSettings(enabled=True)

    prefix = "Value error, enabled audit export requires explicit fields: "
    message = error.value.errors()[0]["msg"]
    assert message.startswith(prefix)
    required_fields = message.removeprefix(prefix).split(", ")

    documented_required_fields = {
        field for field, (default, _description) in _export_setting_rows().items() if default == "required when enabled"
    }
    assert documented_required_fields == set(required_fields)


def test_configuration_documents_live_signer_rotation_policy() -> None:
    rotation_field = LandscapeExportSettings.model_fields["signer_rotation_policy"]
    allowed_policies = {str(policy) for policy in get_args(rotation_field.annotation)}
    assert rotation_field.default in allowed_policies

    default, description = _export_setting_rows()["signer_rotation_policy"]
    assert default == f"`{rotation_field.default}`"
    assert {policy for policy in allowed_policies if f"`{policy}`" in description} == allowed_policies


def test_runbook_binds_every_reconciliation_result_to_a_safe_action() -> None:
    rows = _reconciliation_decision_rows()
    live_names = {kind.name for kind in SinkEffectReconcileKind}
    assert len(rows) == len(live_names)
    assert {name for name, _action in rows} == live_names

    actions = {SinkEffectReconcileKind[name]: _safe_action_categories(action) for name, action in rows}
    assert actions == {
        SinkEffectReconcileKind.NOT_APPLIED: frozenset({"commit_once"}),
        SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR: frozenset({"finalize_without_commit"}),
        SinkEffectReconcileKind.UNKNOWN: frozenset({"block_without_commit"}),
    }
