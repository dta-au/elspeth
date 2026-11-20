"""Safety properties of scripts/staging/backup_vm_recovery.py.

No Docker, sudo or network: a FakeHost plays the subprocess boundary and a
real LocalDirectoryStore writes into tmp_path.
"""

from __future__ import annotations

import dataclasses
import gzip
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts.staging import backup_vm_recovery as backup
from scripts.staging.opslib import CommandFailed, CommandResult, OperationFailed, UsageError, run_tool

POSTGRES = "db-a"
WEB = "web-a"
GATEWAY = "gateway-a"
IDLE = {"composer": 0, "runs": 0, "states": 5, "messages": 9, "blobs": 2}


def physical_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = b"16\n"
        info = tarfile.TarInfo("PG_VERSION")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue())


def container(name: str, *, mounts: list[dict[str, str]], env: list[str] | None = None) -> dict[str, object]:
    return {
        "Id": "id-" + name,
        "Name": "/" + name,
        "Image": "image-" + name,
        "Config": {"Env": env or ["PATH=/usr/bin"]},
        "Mounts": mounts,
        "NetworkSettings": {"Networks": {"net": {"NetworkID": "net-1"}}},
    }


@dataclass
class FakeHost:
    """The subprocess boundary: models paused containers, activity counts and a verification copy."""

    activity: Callable[[str, int], dict[str, int]] = lambda name, index: dict(IDLE)
    free_bytes: int = 10 * 1024**3
    fingerprints: list[str] = field(default_factory=lambda: ["fp-1"] * 4)
    fail_stream: str | None = None
    unpause_fails: bool = False
    unpause_hangs: frozenset[str] = frozenset()
    pause_fails: frozenset[str] = frozenset()
    on_stream: Callable[[str], None] | None = None
    paused: set[str] = field(default_factory=set)
    verifier_accepts_corruption: bool = False
    hmac_in_env: bool = False
    events: list[tuple[str, str]] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    activity_reads: int = 0
    corrupted: bool = False

    def containers(self) -> list[dict[str, object]]:
        env = ["ELSPETH_JUDGE_METADATA_HMAC_KEY=x"] if self.hmac_in_env else None
        return [
            container(
                POSTGRES, mounts=[{"Type": "volume", "Name": "pgdata", "Destination": "/var/lib/postgresql/data", "Source": "/v/pg"}]
            ),
            container(
                WEB,
                env=env,
                mounts=[
                    {"Type": "volume", "Name": "state", "Destination": "/app/state", "Source": "/v/state"},
                    {"Type": "bind", "Name": "", "Destination": "/app/config", "Source": "/srv/config/app.yaml"},
                ],
            ),
            container(GATEWAY, mounts=[]),
        ]

    @staticmethod
    def _strip(argv: Sequence[str]) -> list[str]:
        args = list(argv)
        return args[2:] if args[:2] == ["sudo", "-n"] else args

    def run(self, argv: Sequence[str], *, stdin: bytes | None = None, timeout: float | None = None) -> CommandResult:
        self.calls.append(tuple(argv))
        args = self._strip(argv)
        ok = CommandResult(0, b"", b"")
        if args[:2] == ["docker", "ps"]:
            return CommandResult(0, b"id-db-a\nid-web-a\nid-gateway-a\n", b"")
        if args[:2] == ["docker", "inspect"]:
            wanted = set(args[2:])
            found = [item for item in self.containers() if item["Id"] in wanted or str(item["Name"]).lstrip("/") in wanted]
            return CommandResult(0, json.dumps(found).encode(), b"")
        if args[:3] in (["docker", "image", "inspect"], ["docker", "network", "inspect"]):
            return CommandResult(0, b"[{}]", b"")
        if args[:3] == ["docker", "volume", "inspect"]:
            return CommandResult(0, json.dumps([{"Name": name, "Mountpoint": "/v/" + name} for name in args[3:]]).encode(), b"")
        if args[:2] == ["docker", "pause"]:
            self.events.append(("pause", args[2]))
            if args[2] in self.pause_fails:
                return CommandResult(1, b"", b"pause failed")
            self.paused.add(args[2])
            return ok
        if args[:2] == ["docker", "unpause"]:
            self.events.append(("unpause", args[2]))
            if args[2] in self.unpause_hangs:
                # What subprocess.run(timeout=60) raises when docker hangs.
                raise subprocess.TimeoutExpired(list(argv), timeout or 0)
            if args[2] not in self.paused:
                return CommandResult(1, b"", b"container is not paused")
            if self.unpause_fails:
                return CommandResult(1, b"", b"busy")
            self.paused.discard(args[2])
            return ok
        if args[:3] == ["docker", "container", "inspect"]:
            return CommandResult(0, b"true\n" if args[-1] in self.paused else b"false\n", b"")
        if args[:2] == ["docker", "exec"] and backup.ACTIVITY_SQL in args:
            name = args[2]
            value = self.activity(name, self.activity_reads)
            self.activity_reads += 1
            self.events.append(("activity", json.dumps(value, sort_keys=True)))
            return CommandResult(0, json.dumps(value).encode(), b"")
        if args[:2] == ["docker", "exec"] and "pg_database" in args[-1]:
            return CommandResult(0, b"elspeth_sessions\n", b"")
        if args[:2] == ["docker", "run"]:
            self.events.append(("pg_verifybackup", args[-1]))
            accepted = not self.corrupted or self.verifier_accepts_corruption
            return ok if accepted else CommandResult(1, b"", b"checksum mismatch")
        if args[:3] == ["python3", "-B", "-c"]:
            code = args[3]
            if code == backup.DISK_FREE:
                return CommandResult(0, str(self.free_bytes).encode(), b"")
            if code == backup.SOURCE_FINGERPRINT:
                return CommandResult(0, self.fingerprints.pop(0).encode(), b"")
            if code == backup.CORRUPT_PG_VERSION:
                self.corrupted = True
                return CommandResult(0, b"31360a", b"")
            if code == backup.RESTORE_PG_VERSION:
                self.corrupted = False
                return ok
            if code == backup.REMOVE_TREE:
                self.events.append(("remove_copy", args[4]))
                return ok
        if args[:1] == ["git"]:
            return ok
        raise AssertionError(f"unexpected command {args}")

    def stream(self, argv: Sequence[str], *, gzip: bool = False) -> Iterator[bytes]:
        self.calls.append(tuple(argv))
        args = self._strip(argv)
        label = " ".join(args)
        self.events.append(("stream", label))
        if self.on_stream is not None:
            self.on_stream(label)
        if self.fail_stream is not None and self.fail_stream in label:
            yield b"partial"
            raise CommandFailed(argv, 3, b"disk full")
        yield physical_archive() if "pg_basebackup" in args else b"artifact-bytes"

    def feed(self, argv: Sequence[str], chunks: Iterator[bytes], *, timeout: float = 300.0) -> bytes:
        self.calls.append(tuple(argv))
        consumed = b"".join(chunks)
        self.events.append(("feed", " ".join(self._strip(argv))[:40]))
        assert consumed
        return b""


def make_capture(tmp_path: Path, host: FakeHost, **overrides: object) -> backup.RecoveryCapture:
    destination = tmp_path / "destination"
    destination.mkdir(exist_ok=True)
    (tmp_path / "etc").mkdir(exist_ok=True)
    base = backup.BackupConfig(
        postgres=(POSTGRES,),
        writers=(WEB, GATEWAY),
        verification_container=WEB,
        prefix="deployment/recovery",
        lock_file=tmp_path / "lock",
        host_config_root=tmp_path / "etc",
        drain_poll_seconds=0.0,
    )
    config = dataclasses.replace(base, **overrides)
    store = backup.LocalDirectoryStore(destination, repo_root=tmp_path / "repo")
    return backup.RecoveryCapture(config=config, runner=host, store=store, sleep=lambda seconds: None, label="label")


def index_of(events: list[tuple[str, str]], kind: str) -> int:
    return next(position for position, (event, _detail) in enumerate(events) if event == kind)


def test_waits_for_drain_before_pausing_writers(tmp_path: Path) -> None:
    def activity(name: str, index: int) -> dict[str, int]:
        # Reads 0 (start) and 1, 2 (drain polls) still show running work.
        return {**IDLE, "composer": 1} if index in (1, 2) else dict(IDLE)

    host = FakeHost(activity=activity)
    make_capture(tmp_path, host).run()

    busy = [position for position, (event, detail) in enumerate(host.events) if event == "activity" and '"composer": 1' in detail]
    assert busy, "control: the fake reported active work"
    assert index_of(host.events, "pause") > max(busy)


def test_activity_change_after_pause_aborts_and_unpauses(tmp_path: Path) -> None:
    def activity(name: str, index: int) -> dict[str, int]:
        # Read 2 is the recheck after pausing: a message landed between drain and pause.
        return {**IDLE, "messages": 10} if index >= 2 else dict(IDLE)

    host = FakeHost(activity=activity)
    with pytest.raises(OperationFailed, match="activity changed before writers were paused"):
        make_capture(tmp_path, host).run()

    assert [detail for event, detail in host.events if event == "unpause"] == [GATEWAY, WEB]
    assert not any(event == "stream" and "pg_dump" in detail for event, detail in host.events)


def test_writers_unpaused_when_capture_fails_and_no_partial_artifact(tmp_path: Path) -> None:
    host = FakeHost(fail_stream="pg_dump")
    with pytest.raises(OperationFailed, match="details retained"):
        make_capture(tmp_path, host).run()

    assert [detail for event, detail in host.events if event == "unpause"] == [GATEWAY, WEB]
    base = tmp_path / "destination" / "deployment" / "recovery" / "label"
    assert not (base / "postgres" / POSTGRES / "elspeth_sessions.dump").exists()
    assert not list(base.rglob("*.partial"))
    assert list(base.glob("failed-command-*.json"))


def test_unpause_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    host = FakeHost(unpause_fails=True)
    with pytest.raises(OperationFailed, match="need a manual unpause"):
        make_capture(tmp_path, host).run()


def test_unpause_timeout_still_resumes_every_other_writer_and_names_the_stuck_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost(fail_stream="pg_dump", unpause_hangs=frozenset({GATEWAY}))
    with pytest.raises(OperationFailed, match="need a manual unpause"):
        make_capture(tmp_path, host).run()

    assert host.paused == {GATEWAY}
    assert [detail for event, detail in host.events if event == "unpause"] == [GATEWAY, GATEWAY, GATEWAY, WEB]
    captured = capsys.readouterr()
    stuck = next(json.loads(line) for line in captured.out.splitlines() if '"writers_still_paused"' in line)
    assert stuck["containers"] == [GATEWAY]
    assert stuck["manual_command"] == f"sudo -n docker unpause {GATEWAY}"
    assert f"docker unpause {GATEWAY}" in captured.err


def test_failure_mid_pause_unpauses_the_writers_already_listed(tmp_path: Path) -> None:
    host = FakeHost(pause_fails=frozenset({GATEWAY}))
    with pytest.raises(OperationFailed, match="exited 1"):
        make_capture(tmp_path, host).run()

    # The gateway pause failed, so its unpause is refused; its state shows it is not paused.
    assert host.paused == set()
    assert [detail for event, detail in host.events if event == "unpause"] == [GATEWAY, WEB]


def test_a_stop_signal_during_capture_unpauses_every_writer(tmp_path: Path) -> None:
    previous = {number: signal.getsignal(number) for number in backup.STOP_SIGNALS}

    def hang_up(label: str) -> None:
        if "pg_dump" in label:
            os.kill(os.getpid(), signal.SIGHUP)

    host = FakeHost(on_stream=hang_up)
    try:
        backup.install_stop_handlers()
        with pytest.raises(KeyboardInterrupt, match="signal"):
            make_capture(tmp_path, host).run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    assert set(backup.STOP_SIGNALS) == {signal.SIGTERM, signal.SIGHUP}
    assert host.paused == set()
    assert [detail for event, detail in host.events if event == "unpause"] == [GATEWAY, WEB]


def test_physical_verification_not_run_when_disk_is_short(tmp_path: Path) -> None:
    host = FakeHost(free_bytes=1024)
    pointer = make_capture(tmp_path, host).run()

    assert pointer["physical_verification_not_run"] == [POSTGRES]
    assert pointer["physical_verification_passed"] == []
    assert not any(event == "pg_verifybackup" for event, _detail in host.events)
    manifest = json.loads((tmp_path / "destination" / "deployment" / "recovery" / "label" / "manifest.json").read_text())
    assert manifest["physical_verification"][POSTGRES]["pg_verifybackup"] == "not_run_insufficient_local_space"


def test_physical_verification_passes_with_corruption_negative_control(tmp_path: Path) -> None:
    host = FakeHost()
    pointer = make_capture(tmp_path, host).run()

    assert pointer["physical_verification_passed"] == [POSTGRES]
    assert [event for event, _detail in host.events if event == "pg_verifybackup"] == ["pg_verifybackup"] * 3
    assert any(event == "remove_copy" for event, _detail in host.events)
    current = json.loads((tmp_path / "destination" / "deployment" / "recovery" / "CURRENT.json").read_text())
    assert current["status"] == "complete"


def test_verifier_that_accepts_corruption_fails_the_run(tmp_path: Path) -> None:
    host = FakeHost(verifier_accepts_corruption=True)
    with pytest.raises(OperationFailed, match="accepted a corrupted copy"):
        make_capture(tmp_path, host).run()
    assert not (tmp_path / "destination" / "deployment" / "recovery" / "CURRENT.json").exists()
    assert any(event == "remove_copy" for event, _detail in host.events)


def test_refuses_to_back_up_a_container_holding_the_hmac_key(tmp_path: Path) -> None:
    host = FakeHost(hmac_in_env=True)
    with pytest.raises(OperationFailed, match="HMAC key"):
        make_capture(tmp_path, host).run()
    assert not any(event == "pause" for event, _detail in host.events)


def test_source_change_during_archive_aborts(tmp_path: Path) -> None:
    host = FakeHost(fingerprints=["before", "after"])
    with pytest.raises(OperationFailed, match="changed during its archive"):
        make_capture(tmp_path, host, source_root=tmp_path / "checkout").run()


def test_source_fingerprint_script_sees_a_change(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "keep").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / "keep" / "file.txt").write_text("one")

    def fingerprint() -> str:
        argv = [sys.executable, "-B", "-c", backup.SOURCE_FINGERPRINT, str(root), json.dumps([".venv"])]
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()

    first = fingerprint()
    (root / ".venv" / "ignored").write_text("cache")
    assert fingerprint() == first, "an excluded directory must not move the fingerprint"
    (root / "keep" / "file.txt").write_text("two!")
    assert fingerprint() != first


def test_local_store_refuses_unsafe_keys_and_repository_destinations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "inside").mkdir(parents=True)
    with pytest.raises(UsageError, match="outside the repository"):
        backup.LocalDirectoryStore(repo / "inside", repo_root=repo)
    store = backup.LocalDirectoryStore(tmp_path, repo_root=repo)
    for key in ("../escape", "/abs", "a//b", "a/./b", "a/b c"):
        with pytest.raises(OperationFailed, match="safe relative path"):
            store.write_stream(key, [b"x"])
    record = store.write_stream("a/b.bin", [b"one", b"two"])
    assert record["bytes"] == 6 and record["full_remote_readback_verified"] is True
    with pytest.raises(OperationFailed, match="already exists"):
        store.write_stream("a/b.bin", [b"again"])
    assert (tmp_path / "a" / "b.bin").read_bytes() == b"onetwo"


def test_dry_run_runs_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host = FakeHost()
    args = ["--postgres", POSTGRES, "--writer", WEB, "--writer", GATEWAY, "--verification-container", WEB]
    args += ["--destination", str(tmp_path), "--prefix", "deployment/recovery", "--lock-file", str(tmp_path / "lock")]
    assert backup.main(args, runner=host) == 0
    assert host.calls == []
    assert '"dry_run"' in capsys.readouterr().out


def test_missing_deployment_value_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "ELSPETH_OPS_BACKUP_PREFIX",
        "ELSPETH_OPS_BACKUP_LOCK",
        "ELSPETH_OPS_BACKUP_DESTINATION",
        "ELSPETH_OPS_VERIFICATION_CONTAINER",
    ):
        monkeypatch.delenv(name, raising=False)
    args = ["--postgres", POSTGRES, "--writer", WEB, "--verification-container", WEB, "--destination", str(tmp_path)]
    assert run_tool(backup.main, args) == 2


def test_lock_held_by_another_backup_refuses(tmp_path: Path) -> None:
    import fcntl

    lock = tmp_path / "lock"
    lock.write_bytes(b"")
    destination = tmp_path / "destination"
    destination.mkdir()
    args = ["--postgres", POSTGRES, "--writer", WEB, "--verification-container", WEB, "--destination", str(destination)]
    args += ["--prefix", "p", "--lock-file", str(lock), "--execute"]
    host = FakeHost()
    with lock.open("r+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(OperationFailed, match="holds the lock"):
            backup.main(args, runner=host, store_factory=lambda root: backup.LocalDirectoryStore(root, repo_root=tmp_path / "repo"))
    assert host.calls == []


def test_help_needs_no_environment() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.staging.backup_vm_recovery", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(backup.__file__).resolve().parents[2])},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--execute" in result.stdout
