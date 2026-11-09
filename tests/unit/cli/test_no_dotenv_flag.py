"""The CLI's ``.env`` discovery follows the working directory (GitHub issue #353).

The CLI is invoked in-process, so ``dotenv_free_environment`` first removes
what pytest-dotenv loaded from the checkout's own ``.env``; every ``.env`` these
tests write lives in ``tmp_path``.  The probe is where
``composer users bootstrap-admin`` puts the sessions store: the ``.env`` names
``ELSPETH_WEB__SESSION_DB_URL``, so a honoured file moves the store out of
``--data-dir``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from typer.testing import CliRunner

import elspeth.cli
from elspeth.cli import app

pytestmark = pytest.mark.usefixtures("dotenv_free_environment")

_SENTINEL = "ELSPETH_TEST_ISSUE_353_DOTENV_SENTINEL"


def _write_dotenv(directory: Path) -> tuple[Path, Path]:
    """Write a ``.env`` that relocates the sessions store; return (file, relocated store)."""
    relocated_store = directory / "from_dotenv_sessions.db"
    dotenv_file = directory / ".env"
    dotenv_file.write_text(
        f"ELSPETH_WEB__SESSION_DB_URL=sqlite:///{relocated_store}\n{_SENTINEL}=loaded\n",
        encoding="utf-8",
    )
    return dotenv_file, relocated_store


def _nearest_dotenv(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _invoke(arguments: list[str], watched: tuple[str, ...]) -> tuple[int, str, dict[str, str | None]]:
    """Invoke the CLI; return (exit code, output, the watched variables as seen inside the call)."""
    # ``load_dotenv`` writes ``os.environ`` directly; restore it so a honoured
    # file cannot leak into later tests.
    with patch.dict(os.environ, os.environ.copy(), clear=True):
        result = CliRunner().invoke(app, arguments)
        return result.exit_code, result.output, {key: os.environ.get(key) for key in watched}


def _bootstrap_admin(global_options: list[str], data_dir: Path) -> tuple[int, str, dict[str, str | None]]:
    arguments = [
        *global_options,
        "composer",
        "users",
        "bootstrap-admin",
        "local",
        "operator",
        "--note",
        "issue 353 dotenv regression",
        "--data-dir",
        str(data_dir),
    ]
    return _invoke(arguments, (_SENTINEL, "ELSPETH_WEB__SESSION_DB_URL"))


@pytest.mark.parametrize(
    "global_options",
    [
        pytest.param(["--no-dotenv"], id="cwd-dotenv"),
        pytest.param(["--no-dotenv", "--env-file", ".env"], id="cwd-dotenv-and-explicit-env-file"),
    ],
)
def test_no_dotenv_ignores_a_dotenv_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_options: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _dotenv_file, relocated_store = _write_dotenv(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    exit_code, output, seen = _bootstrap_admin(global_options, data_dir)

    assert exit_code == 0, output
    assert seen == {_SENTINEL: None, "ELSPETH_WEB__SESSION_DB_URL": None}
    assert (data_dir / "sessions.db").is_file()
    assert not relocated_store.exists()


def test_without_no_dotenv_the_working_directory_dotenv_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the file the test above ignores is discovered from the cwd once the flag is absent."""
    monkeypatch.chdir(tmp_path)
    _dotenv_file, relocated_store = _write_dotenv(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    exit_code, output, seen = _bootstrap_admin([], data_dir)

    assert exit_code == 0, output
    assert seen == {_SENTINEL: "loaded", "ELSPETH_WEB__SESSION_DB_URL": f"sqlite:///{relocated_store}"}
    assert relocated_store.is_file()
    assert not (data_dir / "sessions.db").exists()


def test_explicit_env_file_is_honoured_without_no_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    dotenv_file, relocated_store = _write_dotenv(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    exit_code, output, seen = _bootstrap_admin(["--env-file", str(dotenv_file)], data_dir)

    assert exit_code == 0, output
    assert seen == {_SENTINEL: "loaded", "ELSPETH_WEB__SESSION_DB_URL": f"sqlite:///{relocated_store}"}
    assert relocated_store.is_file()


def test_a_dotenv_beside_the_package_is_not_loaded_from_another_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``.env`` found by walking up from ``elspeth/cli.py`` is not the cwd's and must stay unloaded.

    Uses ``plugins list`` so a regression cannot reach whatever store that
    file names.  Needs a ``.env`` above the package (an editable checkout that
    has one); without it there is nothing for discovery to wrongly find.
    """
    beside_package = _nearest_dotenv(Path(elspeth.cli.__file__).resolve().parent)
    if beside_package is None:
        pytest.skip("no .env above the installed elspeth package to exclude")
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    assert _nearest_dotenv(empty_cwd) is None, "precondition: no .env above the tmp cwd"
    monkeypatch.chdir(empty_cwd)
    watched = tuple(key for key in dotenv_values(beside_package) if key not in os.environ)
    assert watched, "precondition: the beside-package .env sets a variable absent from the test environment"

    exit_code, output, seen = _invoke(["plugins", "list"], watched)

    assert exit_code == 0, output
    # Compare names only: the file may hold real credentials, and an
    # assertion diff of values would print them.
    assert sorted(key for key, value in seen.items() if value is not None) == []
