"""Current pre-release session-store cutover documentation contract."""

import re
from pathlib import Path
from typing import Any

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import key_derivation
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

_RUNBOOK = Path("docs/runbooks/staging-session-db-recreation.md")


def test_current_cutover_and_verification_use_live_schema_epochs() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    current_cutover = runbook.split("## Current Cutover:", maxsplit=1)[1].split("## Prior Cutover:", maxsplit=1)[0]
    current_procedure = current_cutover.split("### Current Procedure", maxsplit=1)[1]
    session_expectations = re.findall(
        r'^sqlite3 -readonly "\$DB_PATH" \'PRAGMA user_version;\'\s+# expect (\d+) \(== SESSION_SCHEMA_EPOCH\)$',
        current_procedure,
        flags=re.MULTILINE,
    )
    landscape_expectations = re.findall(
        r'^sqlite3 -readonly "\$LANDSCAPE_PATH" \'PRAGMA user_version;\'\s+# expect (\d+) \(== SQLITE_SCHEMA_EPOCH\)$',
        current_procedure,
        flags=re.MULTILINE,
    )

    assert f"session epoch {SESSION_SCHEMA_EPOCH}" in current_cutover
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH}" in current_cutover
    assert f"session-epoch-{SESSION_SCHEMA_EPOCH}/Landscape-epoch-{SQLITE_SCHEMA_EPOCH} result" in current_cutover
    assert "Unsupported stores remain unchanged" in current_cutover
    assert "explicitly new database target" in current_cutover
    assert "object-free, unstamped target" in current_cutover
    assert "do not run any\n   historical reset procedure below" in current_procedure
    assert "reset, deletion, and recreation instructions are not applicable" in current_cutover
    assert session_expectations == [str(SESSION_SCHEMA_EPOCH)]
    assert landscape_expectations == [str(SQLITE_SCHEMA_EPOCH)]
    assert "guided" not in runbook.lower()
    assert "freeform Build step" in runbook
    assert "inspect its recorded audit story" in runbook


def test_replica_schema_cutover_belongs_to_0_8_1() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    release_0_8_1, release_0_8_0 = changelog.split("## 0.8.1 -", maxsplit=1)[1].split("## 0.8.0 -", maxsplit=1)
    release_0_8_0 = release_0_8_0.split("\n## ", maxsplit=1)[0]

    assert "## Current Cutover: 0.8.10 candidate" in runbook
    assert "## Prior Cutover: 0.8.6 candidate" in runbook
    assert "## Prior Cutover: 0.8.3 candidate (session epoch 72, Landscape epoch 49)" in runbook
    assert "## Prior Cutover: 0.8.1" in runbook
    assert "install 0.8.1" in runbook
    assert "`SESSION_SCHEMA_EPOCH` advances from 53\nto 71" in release_0_8_1
    assert "`SQLITE_SCHEMA_EPOCH` advances from 38 to 49" in release_0_8_1
    assert "then install 0.8.1" in release_0_8_1
    assert "`SESSION_SCHEMA_EPOCH` advances from 35\nto 53" in release_0_8_0
    assert "`SQLITE_SCHEMA_EPOCH` advances from 29 to 38" in release_0_8_0


def test_historical_release_epoch_is_independent_of_future_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    test_replica_schema_cutover_belongs_to_0_8_1()
    monkeypatch.setattr(f"{__name__}.SQLITE_SCHEMA_EPOCH", SQLITE_SCHEMA_EPOCH + 1)
    test_replica_schema_cutover_belongs_to_0_8_1()


def test_current_runbook_still_requires_current_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    test_current_cutover_and_verification_use_live_schema_epochs()
    monkeypatch.setattr(f"{__name__}.SQLITE_SCHEMA_EPOCH", SQLITE_SCHEMA_EPOCH + 1)
    with pytest.raises(AssertionError):
        test_current_cutover_and_verification_use_live_schema_epochs()


def test_historical_prose_mutation_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text
    marker = "`SQLITE_SCHEMA_EPOCH` advances from 38 to 49"
    changelog = Path("CHANGELOG.md").resolve()
    assert original(changelog).count(marker) == 1

    def changed(path: Path, *args: Any, **kwargs: Any) -> str:
        text = original(path, *args, **kwargs)
        return text.replace(marker, "`SQLITE_SCHEMA_EPOCH` advances from 38 to 50") if path.resolve() == changelog else text

    monkeypatch.setattr(Path, "read_text", changed)
    with pytest.raises(AssertionError):
        test_replica_schema_cutover_belongs_to_0_8_1()


def test_key_derivation_docstring_cites_a_runbook_section_that_exists() -> None:
    """The derived-key docstring's operator pointer must resolve.

    ``key_derivation``'s docstring sends the operator to a named section of
    this runbook for what to do about the values that change at the epoch
    boundary. That pointer once named guidance the runbook did not contain,
    which is invisible until an operator follows it mid-cutover and finds
    nothing. Renaming the heading without updating the docstring -- or the
    reverse -- reds here instead.
    """
    docstring = key_derivation.__doc__
    assert docstring is not None
    pointer = re.search(r'"([^"]+)" in\s+``(docs/runbooks/[^`]+)``', docstring)
    assert pointer is not None, "the docstring no longer cites a runbook section by heading and path"
    section, path = pointer.group(1), pointer.group(2)

    assert Path(path) == _RUNBOOK
    assert f"### {section}\n" in _RUNBOOK.read_text(encoding="utf-8")
