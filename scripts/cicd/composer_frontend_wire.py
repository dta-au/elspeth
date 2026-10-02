"""Shared producer/fixture census; frontend runtime dispatch is verified by Vitest.

The generated artifact supplies registry membership without a Node dependency.
Reading it is source extraction, never evidence that frontend tests executed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elspeth.web.composer.redaction import MANIFEST, ToolRedaction, _SensitiveMarker, walk_model_schema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = PROJECT_ROOT / "src/elspeth/web/frontend/src/test/fixtures/redacted-tool-arguments.json"
REGENERATE_HINT = (
    "Run '.venv/bin/python scripts/cicd/bootstrap_proposal_diff_fixture.py --write' "
    "to regenerate, then review the diff AND the frontend tests that read the "
    "changed cases (src/elspeth/web/frontend/src/**/*.test.ts*) before merging: "
    "a grammar change here can make a frontend projection unreachable without "
    "reddening any frontend test."
)


class FrontendCensusError(ValueError):
    """The required generated source cannot provide an unambiguous census."""


@dataclass(frozen=True)
class FrontendWireRow:
    tool: str
    projected: bool
    fixture_cases: frozenset[str]
    summarizes_argument: bool
    source: str

    @property
    def fixture_required(self) -> bool:
        return self.projected and self.summarizes_argument

    @property
    def missing_fixture(self) -> bool:
        return self.fixture_required and not self.fixture_cases


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrontendCensusError(f"Duplicate fixture object key: {key}")
        result[key] = value
    return result


def load_fixture(path: Path | None = None) -> dict[str, Any]:
    source = FIXTURE_PATH if path is None else path
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise FrontendCensusError(f"Cannot load frontend fixture {source}: {exc}. {REGENERATE_HINT}") from exc
    if not isinstance(loaded, dict):
        raise FrontendCensusError(f"Frontend fixture root must be an object: {source}")
    return loaded


def _fixture_cases(loaded: dict[str, Any]) -> dict[str, Any]:
    try:
        cases = loaded["cases"]
    except KeyError as exc:
        raise FrontendCensusError("Frontend fixture is missing cases") from exc
    if not isinstance(cases, dict) or not cases:
        raise FrontendCensusError("Frontend fixture must record at least one case")
    for name, case in cases.items():
        if not isinstance(case, dict) or "tool" not in case or not isinstance(case["tool"], str):
            raise FrontendCensusError(f"Frontend fixture case {name} must name a tool")
        if "arguments" not in case or "redacted" not in case:
            raise FrontendCensusError(f"Frontend fixture case {name} must record arguments and redacted payload")
    return cases


def fixture_cases(path: Path | None = None) -> dict[str, Any]:
    return _fixture_cases(load_fixture(path))


def _projected_tools(loaded: dict[str, Any]) -> frozenset[str]:
    try:
        names = loaded["projected_tools"]
    except KeyError as exc:
        raise FrontendCensusError("Frontend fixture is missing projected_tools") from exc
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise FrontendCensusError("Frontend projected_tools must be a nonempty list of names")
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise FrontendCensusError(f"Duplicate projected tools: {sorted(duplicates)}")
    return frozenset(names)


def projected_tools(path: Path | None = None) -> frozenset[str]:
    return _projected_tools(load_fixture(path))


def summarizes_an_argument(entry: ToolRedaction) -> bool:
    """Measure argument summaries only; response summaries do not affect cards."""
    if entry.argument_model is not None:
        return any(
            any(isinstance(marker, _SensitiveMarker) for marker in node.metadata) for node in walk_model_schema(entry.argument_model)
        )
    assert entry.policy is not None  # ToolRedaction invariant
    return bool(entry.policy.sensitive_argument_keys)


def census_frontend_wire(
    *,
    fixture_path: Path | None = None,
    manifest: Mapping[str, ToolRedaction] | None = None,
) -> dict[str, FrontendWireRow]:
    entries = MANIFEST if manifest is None else manifest
    loaded = load_fixture(fixture_path)
    cases = _fixture_cases(loaded)
    projected = _projected_tools(loaded)
    unknown_projected = projected - entries.keys()
    unknown_cases = {name: case["tool"] for name, case in cases.items() if case["tool"] not in entries}
    if unknown_projected or unknown_cases:
        raise FrontendCensusError(f"Frontend tools absent from MANIFEST: projected={sorted(unknown_projected)}, cases={unknown_cases}")
    source = str(FIXTURE_PATH if fixture_path is None else fixture_path)
    return {
        name: FrontendWireRow(
            tool=name,
            projected=name in projected,
            fixture_cases=frozenset(case_name for case_name, case in cases.items() if case["tool"] == name),
            summarizes_argument=summarizes_an_argument(entry),
            source=source,
        )
        for name, entry in entries.items()
    }
