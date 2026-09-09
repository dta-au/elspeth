"""Current pre-release session-store cutover documentation contract."""

import re
from pathlib import Path

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import key_derivation
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

_RUNBOOK = Path("docs/runbooks/staging-session-db-recreation.md")


def test_current_cutover_and_verification_use_live_schema_epochs() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    current_cutover = runbook.split("## Current Cutover:", maxsplit=1)[1].split("## Historical Cutover:", maxsplit=1)[0]
    current_procedure = runbook.split("### Procedure", maxsplit=1)[1].split(
        "#### 0.7.0 epoch + smoke verification",
        maxsplit=1,
    )[0]
    session_expectations = re.findall(
        r'^sqlite3 "\$DB_PATH" \'PRAGMA user_version;\'\s+# expect (\d+) \(== SESSION_SCHEMA_EPOCH\)$',
        current_procedure,
        flags=re.MULTILINE,
    )
    landscape_expectations = re.findall(
        r'^sqlite3 "\$LANDSCAPE_PATH" \'PRAGMA user_version;\'\s+# expect (\d+) \(== SQLITE_SCHEMA_EPOCH\)$',
        current_procedure,
        flags=re.MULTILINE,
    )

    assert f"session epoch {SESSION_SCHEMA_EPOCH}" in current_cutover
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH}" in current_cutover
    assert f"session-epoch-{SESSION_SCHEMA_EPOCH}/Landscape-epoch-{SQLITE_SCHEMA_EPOCH} record" in current_cutover
    assert f"repair the epoch-{SESSION_SCHEMA_EPOCH} release forward" in current_cutover
    assert session_expectations == [str(SESSION_SCHEMA_EPOCH)]
    assert landscape_expectations == [str(SQLITE_SCHEMA_EPOCH)]


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
