"""Current pre-release session-store cutover documentation contract."""

from pathlib import Path

from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH


def test_current_cutover_requires_live_epoch_contract_and_forbids_downgrade_repair() -> None:
    runbook = Path("docs/runbooks/staging-session-db-recreation.md").read_text(encoding="utf-8")
    current_cutover = runbook.split("## Current Cutover:", maxsplit=1)[1].split("## Historical Cutover:", maxsplit=1)[0]
    normalized = " ".join(runbook.split())

    assert "0.7.2 blob cleanup, guided decline, and row_union barrier" in current_cutover
    assert f"session epoch {SESSION_SCHEMA_EPOCH}" in current_cutover
    assert "Landscape epoch 30" in current_cutover
    assert f"0.7.2 advances `SESSION_SCHEMA_EPOCH` from 35 to {SESSION_SCHEMA_EPOCH}" in current_cutover
    assert "0.7.1 advances the session store from epoch 26 through epoch 35" in current_cutover
    assert "blob-deletion" in current_cutover
    assert "tombstone unlink or directory fsync fails remains retryable" in current_cutover
    assert "exclusive guided-confirmation proposal admission" in current_cutover
    assert "ordinary guided-plan decline settlement" in current_cutover
    assert "quota_exceeded" in current_cutover
    assert "stable HTTP 413" in current_cutover
    assert "restore the epoch-29 database" not in current_cutover.lower()
    assert "downgrade to epoch 29" not in current_cutover.lower()
    assert "Do not restore predecessor source or databases as the repair path." in normalized

    # The verification procedure lives outside the "Current Cutover" section, so
    # bind it to the live constant separately: the section-scoped assertions
    # above are what previously let the PRAGMA probe drift a whole epoch behind.
    assert f"# expect {SESSION_SCHEMA_EPOCH} (== SESSION_SCHEMA_EPOCH)" in runbook
