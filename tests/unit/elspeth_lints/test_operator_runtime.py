"""Protected operator admission must not trust ordinary Python startup state."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from elspeth_lints.core.operator_runtime import (
    OPERATOR_ENTRYPOINT,
    OperatorRuntimeEvidence,
    OperatorRuntimeUnavailableError,
    _admit_native_runtime,
    _decode_native_handoff,
    _validate_python_startup,
    current_operator_runtime,
    require_operator_runtime,
    runtime_private_directory,
    validate_artifact_manifest,
    validate_protected_path,
)


def test_ordinary_python_has_no_operator_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_PROTECTED_OPERATOR_RUNTIME", "1")
    assert current_operator_runtime() is None
    with pytest.raises(OperatorRuntimeUnavailableError, match="protected operator"):
        require_operator_runtime()


def test_operator_entrypoint_is_fixed_and_outside_repository() -> None:
    assert OPERATOR_ENTRYPOINT == "/usr/local/libexec/elspeth-operator"


def _native_packet(*, selected_name: str = "LD_PRELOAD", secret: bytes = "clé-秘密".encode(), python_minor: int = 12) -> bytes:
    def integer(value: int) -> bytes:
        return struct.pack("!I", value)

    def string(value: bytes) -> bytes:
        return integer(len(value)) + value

    arguments = (b"rekey", b"--old-key-env", b"LD_PRELOAD", b"--new-key-env=NEW_KEY")
    return (
        b"ELSPETHOP2\x00"
        + string(b"/opt/elspeth/operator")
        + string(b"/candidate/original-cwd")
        + integer(123)
        + integer(456)
        + integer(python_minor)
        + integer(len(arguments))
        + b"".join(string(argument) for argument in arguments)
        + integer(1)
        + string(selected_name.encode())
        + string(secret)
    )


def test_native_data_preserves_utf8_arbitrary_rekey_name_without_admitting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    handoff = _decode_native_handoff(_native_packet())
    assert handoff.selected_environment == (("LD_PRELOAD", "clé-秘密"),)
    assert handoff.invocation_cwd == Path("/candidate/original-cwd")
    assert "LD_PRELOAD" not in os.environ
    assert "clé-秘密" not in repr(handoff)
    assert current_operator_runtime() is None


@pytest.mark.parametrize("minor", [12, 13])
def test_python_profile_binds_evidence_and_native_handoff(minor: int) -> None:
    evidence = OperatorRuntimeEvidence(Path("/opt/elspeth/operator"), "a" * 64, 123, 456, Path("/candidate"), ("rekey",), minor)
    assert evidence.python_version == (3, minor)
    assert evidence.interpreter == Path(f"/usr/bin/python3.{minor}")
    assert evidence.stdlib == Path(f"/usr/lib/python3.{minor}")
    assert _decode_native_handoff(_native_packet(python_minor=minor)).python_minor == minor


@pytest.mark.parametrize("minor", [0, 11, 14, 313])
def test_native_handoff_refuses_unsupported_python_profile(minor: int) -> None:
    with pytest.raises(OperatorRuntimeUnavailableError, match="Python profile"):
        _decode_native_handoff(_native_packet(python_minor=minor))


@pytest.mark.parametrize("minor", [12, 13])
def test_startup_profile_admission_requires_matching_version_and_exact_import_roots(minor: int) -> None:
    paths = (f"/usr/lib/python3.{minor}", f"/usr/lib/python3.{minor}/lib-dynload", "/opt/elspeth/operator/lib")
    assert _validate_python_startup(minor, version=(3, minor), search_path=paths) == (
        Path(f"/usr/bin/python3.{minor}"),
        Path(f"/usr/lib/python3.{minor}"),
    )
    with pytest.raises(OperatorRuntimeUnavailableError, match="interpreter startup"):
        _validate_python_startup(minor, version=(3, 25 - minor), search_path=paths)
    with pytest.raises(OperatorRuntimeUnavailableError, match="interpreter startup"):
        _validate_python_startup(minor, version=(3, minor), search_path=(*paths, "/candidate"))


def test_bootstrap_uses_only_supported_builtin_version_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = Path(__file__).resolve().parents[3] / "elspeth-lints/operator-runtime/bootstrap.py"
    namespace = runpy.run_path(str(bootstrap))
    select = namespace["_fixed_import_paths"]
    monkeypatch.setenv("ELSPETH_PYTHON_MINOR", "99")
    monkeypatch.setenv("PYTHONHOME", "/candidate")
    for minor in (12, 13):
        assert select(3, minor) == [
            f"/usr/lib/python3.{minor}",
            f"/usr/lib/python3.{minor}/lib-dynload",
            "/opt/elspeth/operator/lib",
        ]
    for major, minor in ((3, 11), (3, 14), (4, 13), (True, 13), (3, True)):
        with pytest.raises(ValueError, match="unsupported"):
            select(major, minor)


@pytest.mark.parametrize(
    "packet",
    [
        _native_packet(selected_name="OPENROUTER_API_KEY"),
        _native_packet(secret=b"\xff"),
        _native_packet() + b"extra",
        _native_packet()[:-1],
        b"forged-envelope",
    ],
)
def test_native_data_rejects_unselected_credentials_and_malformed_packet(packet: bytes) -> None:
    with pytest.raises(OperatorRuntimeUnavailableError):
        _decode_native_handoff(packet)


def test_editable_process_cannot_admit_even_a_well_shaped_handoff() -> None:
    with pytest.raises(OperatorRuntimeUnavailableError, match="startup is not isolated"):
        _admit_native_runtime()
    assert current_operator_runtime() is None


def test_isolated_editable_module_still_cannot_admit_forged_native_descriptor(tmp_path: Path) -> None:
    module = Path(__file__).resolve().parents[3] / "elspeth-lints/src/elspeth_lints/core/operator_runtime.py"
    packet = tmp_path / "synthetic-native-packet"
    packet.write_bytes(_native_packet())
    program = """
import fcntl, importlib.util, os, sys
spec = importlib.util.spec_from_file_location('runtime_probe', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
fd = os.memfd_create('forged-test', os.MFD_ALLOW_SEALING)
os.write(fd, open(sys.argv[2], 'rb').read())
fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SEAL | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_WRITE)
os.dup2(fd, 3)
try:
    module._admit_native_runtime()
except module.OperatorRuntimeUnavailableError:
    assert module.current_operator_runtime() is None
    raise SystemExit(78)
raise SystemExit(1)
"""
    result = subprocess.run(
        ["/usr/bin/python3.12", "-I", "-S", "-B", "-c", program, str(module), str(packet)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78, result.stderr


@pytest.mark.parametrize(
    "mode,uid,accepted",
    [
        (stat.S_IFDIR | 0o700, 123, True),
        (stat.S_IFDIR | 0o755, 123, False),
        (stat.S_IFDIR | 0o700, 456, False),
        (stat.S_IFLNK | 0o700, 123, False),
    ],
)
def test_private_directory_uses_fixed_authenticated_path(monkeypatch: pytest.MonkeyPatch, mode: int, uid: int, accepted: bool) -> None:
    evidence = OperatorRuntimeEvidence(Path("/opt/elspeth/operator"), "a" * 64, 123, 456, Path("/candidate"), ("rekey",))
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.require_operator_runtime", lambda: evidence)
    checked: list[Path] = []
    monkeypatch.setattr(
        "elspeth_lints.core.operator_runtime.validate_protected_path",
        lambda path, **kwargs: checked.append(path),
    )
    monkeypatch.setattr(Path, "lstat", lambda path: os.stat_result((mode, 1, 1, 1, uid, 123, 0, 0, 0, 0)))
    monkeypatch.setenv("TMPDIR", "/candidate/not-trusted")
    if accepted:
        assert runtime_private_directory() == Path("/run/elspeth/operator")
    else:
        with pytest.raises(OperatorRuntimeUnavailableError):
            runtime_private_directory()
    assert checked == [Path("/run/elspeth")]


def test_protected_path_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_lstat = Path.lstat

    def controlled_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path in (Path("/"), Path("/usr")):
            fields = list(metadata)
            fields[4] = 0
            return os.stat_result(fields)
        return metadata

    # The execution sandbox can map OS-owned uid 0 to 65534. This controls the
    # path validator, and does not claim that this host qualifies for signing.
    monkeypatch.setattr(Path, "lstat", controlled_lstat)
    validate_protected_path(Path("/usr"), directory=True)
    with pytest.raises(OperatorRuntimeUnavailableError):
        validate_protected_path(tmp_path, directory=True)
    link = tmp_path / "link"
    link.symlink_to("/usr", target_is_directory=True)
    with pytest.raises(OperatorRuntimeUnavailableError):
        validate_protected_path(link, directory=True)


def test_manifest_inventory_and_hash_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This controls inventory parsing only; temporary files do not qualify an OS artifact.
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.validate_protected_path", lambda *args, **kwargs: None)
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_bytes(b"pass\n")
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "operator_uid": 123,
                "candidate_uid": 456,
                "files": {"bootstrap.py": hashlib.sha256(bootstrap.read_bytes()).hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    assert validate_artifact_manifest(tmp_path) == hashlib.sha256(manifest.read_bytes()).hexdigest()
    bootstrap.write_bytes(b"raise RuntimeError('tampered')\n")
    with pytest.raises(OperatorRuntimeUnavailableError, match="digest"):
        validate_artifact_manifest(tmp_path)
    bootstrap.write_bytes(b"pass\n")
    (tmp_path / "injected.pth").write_bytes(b"import hostile\n")
    with pytest.raises(OperatorRuntimeUnavailableError, match="inventory"):
        validate_artifact_manifest(tmp_path)


@pytest.mark.parametrize("python_minor", [12, 13])
def test_unqualified_native_launcher_refuses_before_python(tmp_path: Path, python_minor: int) -> None:
    project = Path(__file__).resolve().parents[3]
    source = project / "elspeth-lints/operator-runtime/launcher.c"
    executable = tmp_path / "operator-launcher"
    built = subprocess.run(
        [
            "/usr/bin/cc",
            "-static",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-O2",
            f"-DELSPETH_PYTHON_MINOR={python_minor}",
            str(source),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    hostile = tmp_path / "sitecustomize.py"
    marker = tmp_path / "executed"
    hostile.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(tmp_path), LD_PRELOAD="/missing-hostile-library.so")
    refused = subprocess.run(
        [str(executable), "sign-bundle", "relative-bundle.json"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 78
    assert "unqualified" in refused.stderr
    assert "ld.so" not in refused.stderr
    assert not marker.exists()


@pytest.mark.parametrize("minor", [12, 13, 14])
def test_native_build_selects_only_fixed_supported_profile(tmp_path: Path, minor: int) -> None:
    source = Path(__file__).resolve().parents[3] / "elspeth-lints/operator-runtime/launcher.c"
    executable = tmp_path / "profile-probe"
    harness = (
        "#define main protected_launcher_main\n"
        f"#include {json.dumps(str(source))}\n"
        "#undef main\n"
        'int main(void) { printf("%d\\n%s\\n%s\\n", ELSPETH_PYTHON_MINOR, INTERPRETER, STDLIB); return 0; }\n'
    )
    compiled = subprocess.run(
        ["/usr/bin/cc", "-static", "-Wall", "-Wextra", "-Werror", f"-DELSPETH_PYTHON_MINOR={minor}", "-x", "c", "-", "-o", str(executable)],
        input=harness,
        capture_output=True,
        text=True,
        check=False,
    )
    if minor == 14:
        assert compiled.returncode != 0
        assert "must select supported CPython 3.12 or 3.13" in compiled.stderr
        return
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run(
        [str(executable)],
        env={"ELSPETH_PYTHON_MINOR": str(25 - minor)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == f"{minor}\n/usr/bin/python3.{minor}\n/usr/lib/python3.{minor}\n"


def test_make_profile_selection_survives_explicit_cppflags() -> None:
    source = Path(__file__).resolve().parents[3] / "elspeth-lints/operator-runtime"
    result = subprocess.run(
        [
            "/usr/bin/make",
            "--no-print-directory",
            "-n",
            "-C",
            str(source),
            "PYTHON_MINOR=13",
            "CPPFLAGS=-DELSPETH_OPERATOR_UID=123 -DELSPETH_CANDIDATE_UID=456",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "-DELSPETH_PYTHON_MINOR=13" in result.stdout
    assert "-DELSPETH_OPERATOR_UID=123" in result.stdout
    assert "-static" in result.stdout
