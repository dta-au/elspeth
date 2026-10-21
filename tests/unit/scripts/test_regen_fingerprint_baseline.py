"""The legacy baseline command must refuse without loading keys or running code."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

import pytest
from scripts.cicd import regen_fingerprint_baseline


@pytest.mark.parametrize("signing_key", [None, "", "synthetic-signing-key"])
@pytest.mark.parametrize("options", [[], ["--env-file", "unread-operator.env", "--commit"]])
def test_regeneration_is_disabled_before_repository_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signing_key: str | None,
    options: list[str],
) -> None:
    if signing_key is None:
        monkeypatch.delenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", raising=False)
    else:
        monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", signing_key)
    monkeypatch.setattr(sys, "argv", ["regen_fingerprint_baseline.py", *options])
    run = Mock(side_effect=AssertionError("disabled maintenance must not execute subprocesses"))
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(SystemExit) as exc_info:
        regen_fingerprint_baseline.main()

    assert exc_info.value.code == 1
    assert "Baseline regeneration is disabled" in capsys.readouterr().err
    run.assert_not_called()
