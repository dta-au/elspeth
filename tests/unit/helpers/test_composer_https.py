"""Caddy selection controls use local fixtures, never launch a TLS service."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers import composer_https


def test_caddy_is_resolved_once_from_provisioned_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    binary = tools / "caddy"
    binary.write_text("private selection fixture\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    assert composer_https._caddy_executable() == binary.resolve()


@pytest.mark.parametrize("available", [False, True], ids=["missing", "non-executable"])
def test_missing_or_nonexecutable_caddy_refuses_before_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    if available:
        (tmp_path / "caddy").write_text("not executable\n")
        (tmp_path / "caddy").chmod(0o600)
    monkeypatch.setenv("PATH", str(tmp_path))

    def unexpected(*args, **kwargs):
        raise AssertionError("certificate/process acquisition preceded Caddy selection")

    monkeypatch.setattr(composer_https.subprocess, "run", unexpected)
    monkeypatch.setattr(composer_https.subprocess, "Popen", unexpected)
    monkeypatch.setattr(composer_https, "_listener", unexpected)
    directory = tmp_path / "tls"
    with pytest.raises(FileNotFoundError, match="provisioned caddy on PATH"), composer_https.local_composer_tls(None, directory):
        raise AssertionError("missing prerequisite yielded a TLS service")
    assert not directory.exists()


def test_tls_launch_uses_same_resolved_caddy_after_path_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    binary = tools / "caddy"
    binary.write_text("private selection fixture\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    actual_which = composer_https.shutil.which
    lookups: list[str] = []

    def which(name: str) -> str | None:
        lookups.append(name)
        return actual_which(name)

    def certificate(*args, **kwargs):
        monkeypatch.setenv("PATH", str(tmp_path / "empty-after-resolution"))

    class Listener:
        def getsockname(self):
            return ("127.0.0.1", 18471)

        def close(self):
            pass

    class Thread:
        def start(self):
            pass

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

    original = RuntimeError("inert Popen boundary")
    launches: list[list[str]] = []

    def launch(argv, **kwargs):
        launches.append(argv)
        raise original

    monkeypatch.setattr(composer_https.shutil, "which", which)
    monkeypatch.setattr(composer_https.subprocess, "run", certificate)
    monkeypatch.setattr(composer_https.subprocess, "Popen", launch)
    monkeypatch.setattr(composer_https.ssl, "create_default_context", lambda **kwargs: None)
    monkeypatch.setattr(composer_https, "_listener", Listener)
    monkeypatch.setattr(composer_https, "_install_tls_stop", lambda owners: None)
    monkeypatch.setattr(composer_https, "_restore_tls_stop", lambda owners: None)
    monkeypatch.setattr(composer_https.uvicorn, "Config", lambda *args, **kwargs: None)
    monkeypatch.setattr(composer_https.uvicorn, "Server", lambda config: SimpleNamespace(run=lambda: None, should_exit=False))
    monkeypatch.setattr(composer_https.threading, "Thread", lambda *args, **kwargs: Thread())
    with pytest.raises(BaseExceptionGroup) as retained, composer_https.local_composer_tls(None, tmp_path / "tls"):
        raise AssertionError("inert launch unexpectedly yielded")
    assert retained.value.exceptions[0] is original
    assert lookups == ["caddy"]
    assert len(launches) == 1
    assert launches[0][0] == str(binary.resolve())
    assert Path(launches[0][0]).is_absolute()
    assert launches[0][1] == "run"
