"""External digest bindings precede all offline release processing."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from tests.unit.elspeth_lints.test_runtime_artifacts import elf_bytes

from elspeth_lints.core.runtime_artifacts import (
    ROLE_ROOTS,
    ArtifactError,
    ArtifactFile,
    _child_directory,
    _configurations,
    build_release,
    canonical,
    directory_intentions,
    read_archive,
    sha256,
    stage_release,
    verify_release,
    write_archive,
)


def test_wrong_catalog_digest_preserves_output(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_bytes(b"{}")
    output = tmp_path / "release.tar"
    with pytest.raises(ArtifactError, match="digest"):
        build_release(catalog=catalog, expected_sha256="a" * 64, input_root=tmp_path, output=output)
    assert not output.exists()


def test_wrong_release_digest_refuses_before_archive_parse(tmp_path: Path) -> None:
    artifact = tmp_path / "release.tar"
    artifact.write_bytes(b"not an archive")
    with pytest.raises(ArtifactError, match="digest"):
        verify_release(artifact, expected_sha256="a" * 64)


def release_inputs(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    native = elf_bytes()
    inputs: dict[str, dict[str, object]] = {}

    def bind(name: str, raw: bytes) -> dict[str, object]:
        path = tmp_path / name
        path.write_bytes(raw)
        return {"path": name, "size": len(raw), "sha256": sha256(raw)}

    first_party = {
        "elspeth/__init__.py": b"raise RuntimeError('input must never execute')\n",
        "elspeth_lints/__init__.py": b"raise RuntimeError('input must never execute')\n",
        "elspeth_lints/core/operator_runtime.py": b"# supplied fixture\n",
        "elspeth_lints/core/__init__.py": b"# supplied fixture\n",
        "elspeth_lints/core/cli.py": b"# supplied fixture\n",
        "elspeth_lints/core/judge.py": b"# supplied fixture\n",
        "elspeth_lints/core/judge_rpc.py": b"# supplied fixture\n",
        "elspeth_lints/core/judge_supervisor.py": b"# supplied fixture\n",
        "elspeth_lints/core/judge_worker.py": b"# supplied fixture\n",
        "elspeth_lints/core/baseline_rpc.py": b"# supplied fixture\n",
        "elspeth_lints/core/baseline_supervisor.py": b"# supplied fixture\n",
        "elspeth_lints/core/baseline_worker.py": b"# supplied fixture\n",
    }
    for role in ROLE_ROOTS:
        if role == "signer":
            files = {"lib/" + name: ArtifactFile(raw, 0o644) for name, raw in first_party.items()}
            files["bootstrap.py"] = ArtifactFile(b"# supplied bootstrap fixture\n", 0o644)
            files["platform/lib/libfixture.so"] = ArtifactFile(native, 0o644)
        else:
            files = {"usr/lib/python3/dist-packages/" + name: ArtifactFile(raw, 0o644) for name, raw in first_party.items()}
            interpreter = "usr/bin/python3.13" if role.startswith("baseline_") else "usr/bin/python3"
            files[interpreter] = ArtifactFile(native, 0o755)
            files["usr/lib/python3.13/encodings/__init__.py"] = ArtifactFile(b"# stdlib fixture\n", 0o644)
            if role.endswith("supervisor"):
                for name in ("systemd-run", "systemctl"):
                    files["usr/bin/" + name] = ArtifactFile(native, 0o755)
        manifest = {
            "schema": 1,
            "operator_uid": 100,
            "candidate_uid": 200,
            "files": {name: sha256(file.data) for name, file in files.items()},
        }
        files["runtime-manifest.json"] = ArtifactFile(canonical(manifest), 0o644)
        inputs[role] = bind(role + ".tar", write_archive(files))
    inputs["native"] = bind("native.elf", native)
    platform = {
        name: ArtifactFile(raw, mode)
        for name, raw, mode in (
            ("usr/bin/python3.13", native, 0o755),
            ("usr/lib/python3.13/encodings/__init__.py", b"# stdlib fixture\n", 0o644),
            ("usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2", native, 0o755),
            ("usr/bin/git", native, 0o755),
        )
    }
    project = Path(__file__).resolve().parents[3]
    units = {
        role: bind(role + ".service", (project / f"elspeth-lints/{role}-runtime/ubuntu/elspeth-{role}-supervisor.service").read_bytes())
        for role in ("judge", "baseline")
    }
    data: dict[str, object] = {
        "version": 1,
        "profile": "ubuntu-x86_64-offline-v1",
        "python_minor": 13,
        "principals": {
            "operator_uid": 100,
            "operator_gid": 100,
            "candidate_uid": 200,
            "allowed_client_uids": [100, 200],
            "baseline_uid": 300,
            "baseline_gid": 300,
            "workers": {
                name: {"uid": 400 + index, "gid": 400 + index, "authentication": "api"}
                for index, name in enumerate(("openrouter", "claude_agent_sdk", "codex_cli"))
            },
        },
        "inputs": inputs,
        "host_platform": bind("platform.tar", write_archive(platform)),
        "units": units,
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_bytes(canonical(data))
    return catalog, data


def test_complete_fixture_release_is_deterministic_and_stages_without_activation(tmp_path: Path) -> None:
    catalog, _ = release_inputs(tmp_path)
    output = tmp_path / "release.tar"
    identity = build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    repeated = tmp_path / "again.tar"
    assert build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=repeated) == identity
    release = verify_release(output, expected_sha256=identity)
    admission = json.loads(release.files["etc/elspeth/baseline-workers.json"].data)
    assert admission["worker_uid"] == 300
    assert admission["unit_sha256"] == sha256(release.files["etc/systemd/system/elspeth-baseline-supervisor.service"].data)
    assert directory_intentions(release)["run/elspeth/operator"] == (100, 100, 0o700)
    staged = tmp_path / "staged"
    stage_release(output, expected_sha256=identity, root=staged)
    assert (staged / "usr/local/libexec/elspeth-operator").read_bytes() == elf_bytes()
    assert not (staged / "etc/elspeth/judge-workers.json").exists()
    assert (staged / "etc/elspeth/judge-workers.json.pending").is_file()
    assert not (staged / "etc/systemd/system/elspeth-judge-supervisor.service").exists()
    assert (staged / "etc/systemd/system/elspeth-judge-supervisor.service.pending").is_file()
    assert not (staged / "etc/systemd/system/multi-user.target.wants").exists()
    assert not (staged / "var/lib/elspeth-judge/providers/openrouter/provider-environment.json").exists()
    with pytest.raises(ArtifactError, match="absent"):
        stage_release(output, expected_sha256=identity, root=staged)


def test_bound_image_mutation_refuses_before_output(tmp_path: Path) -> None:
    catalog, _ = release_inputs(tmp_path)
    (tmp_path / "signer.tar").write_bytes(b"changed")
    output = tmp_path / "release.tar"
    with pytest.raises(ArtifactError, match="digest"):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["principals", "native", "first_party", "unit"])
def test_rebound_hostile_inputs_still_fail_semantic_controls(tmp_path: Path, mutation: str) -> None:
    catalog, data = release_inputs(tmp_path)
    if mutation == "principals":
        data["principals"]["baseline_uid"] = 100
    elif mutation == "native":
        native = elf_bytes(needed="libfixture.so")
        (tmp_path / "native.elf").write_bytes(native)
        data["inputs"]["native"].update(size=len(native), sha256=sha256(native))
    elif mutation == "first_party":
        image = read_archive((tmp_path / "baseline_worker.tar").read_bytes())
        name = "usr/lib/python3/dist-packages/elspeth/__init__.py"
        image[name] = ArtifactFile(b"# different first-party bytes\n", 0o644)
        manifest = json.loads(image["runtime-manifest.json"].data)
        manifest["files"][name] = sha256(image[name].data)
        image["runtime-manifest.json"] = ArtifactFile(canonical(manifest), 0o644)
        raw = write_archive(image)
        (tmp_path / "baseline_worker.tar").write_bytes(raw)
        data["inputs"]["baseline_worker"].update(size=len(raw), sha256=sha256(raw))
    else:
        raw = (tmp_path / "judge.service").read_bytes() + b"ExecStartPost=/bin/unreviewed\n"
        (tmp_path / "judge.service").write_bytes(raw)
        data["units"]["judge"].update(size=len(raw), sha256=sha256(raw))
    catalog.write_bytes(canonical(data))
    with pytest.raises(ArtifactError):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=tmp_path / "release.tar")
    assert not (tmp_path / "release.tar").exists()


def test_fixture_staging_rejects_symlink_ancestor_without_outside_writes(tmp_path: Path) -> None:
    catalog, _ = release_inputs(tmp_path)
    output = tmp_path / "release.tar"
    identity = build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ArtifactError, OSError)):
        stage_release(output, expected_sha256=identity, root=tmp_path / "alias/staged")
    assert list(outside.iterdir()) == []


def test_release_reverification_rejects_forged_generated_config(tmp_path: Path) -> None:
    catalog, _ = release_inputs(tmp_path)
    output = tmp_path / "release.tar"
    build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    files = read_archive(output.read_bytes())
    name = "etc/elspeth/judge-workers.json"
    forged = json.loads(files[name].data)
    forged["allowed_client_uids"].append(999)
    files[name] = ArtifactFile(canonical(forged), 0o644)
    description = json.loads(files["release.json"].data)
    description["files"][name]["sha256"] = sha256(files[name].data)
    files["release.json"] = ArtifactFile(canonical(description), 0o644)
    output.write_bytes(write_archive(files))
    with pytest.raises(ArtifactError, match="generated release configuration"):
        verify_release(output, expected_sha256=sha256(output.read_bytes()))


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_fixture_intermediate_directories_have_exact_modes_under_umask(tmp_path: Path, mask: int) -> None:
    catalog, _ = release_inputs(tmp_path)
    output = tmp_path / "release.tar"
    identity = build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    staged = tmp_path / "staged"
    previous = os.umask(mask)
    try:
        stage_release(output, expected_sha256=identity, root=staged)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert {stat.S_IMODE(path.stat().st_mode) for path in staged.rglob("*") if path.is_dir()} == {0o755}


def rebind_image(tmp_path: Path, data: dict[str, object], role: str, image: dict[str, ArtifactFile]) -> None:
    manifest = json.loads(image["runtime-manifest.json"].data)
    manifest["files"] = {name: sha256(file.data) for name, file in image.items() if name != "runtime-manifest.json"}
    image["runtime-manifest.json"] = ArtifactFile(canonical(manifest), 0o644)
    raw = write_archive(image)
    (tmp_path / (role + ".tar")).write_bytes(raw)
    data["inputs"][role].update(size=len(raw), sha256=sha256(raw))


@pytest.mark.parametrize(
    "role,module",
    [
        ("signer", "cli"),
        ("judge_supervisor", "judge_supervisor"),
        ("openrouter", "judge_worker"),
        ("claude_agent_sdk", "judge_worker"),
        ("codex_cli", "judge_worker"),
        ("baseline_supervisor", "baseline_supervisor"),
        ("baseline_worker", "baseline_worker"),
    ],
)
def test_fixed_role_entry_module_is_required(tmp_path: Path, role: str, module: str) -> None:
    catalog, data = release_inputs(tmp_path)
    image = read_archive((tmp_path / (role + ".tar")).read_bytes())
    root = "lib" if role == "signer" else "usr/lib/python3/dist-packages"
    del image[f"{root}/elspeth_lints/core/{module}.py"]
    rebind_image(tmp_path, data, role, image)
    catalog.write_bytes(canonical(data))
    with pytest.raises(ArtifactError, match=r"startup|entry"):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=tmp_path / "release.tar")


def test_package_under_nonimportable_root_is_rejected(tmp_path: Path) -> None:
    catalog, data = release_inputs(tmp_path)
    image = read_archive((tmp_path / "judge_supervisor.tar").read_bytes())
    moved = {name.replace("usr/lib/python3/dist-packages/", "arbitrary/nonimportable/"): file for name, file in image.items()}
    rebind_image(tmp_path, data, "judge_supervisor", moved)
    catalog.write_bytes(canonical(data))
    with pytest.raises(ArtifactError, match=r"startup|import"):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=tmp_path / "release.tar")


@pytest.mark.parametrize(
    "role,executable", [("openrouter", "python3"), ("judge_supervisor", "systemd-run"), ("baseline_supervisor", "systemctl")]
)
def test_fixed_executable_must_be_executable(tmp_path: Path, role: str, executable: str) -> None:
    catalog, data = release_inputs(tmp_path)
    image = read_archive((tmp_path / (role + ".tar")).read_bytes())
    name = "usr/bin/" + executable
    image[name] = ArtifactFile(image[name].data, 0o644)
    rebind_image(tmp_path, data, role, image)
    catalog.write_bytes(canonical(data))
    with pytest.raises(ArtifactError, match="executable"):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=tmp_path / "release.tar")


@pytest.mark.parametrize(
    "addition", [b" ExecStartPre=/usr/bin/unreviewed\n", b" EnvironmentFile=/unreviewed\n", b"RootDirectory=/\n", b"BindPaths=/home\n"]
)
def test_fixed_unit_rejects_whitespace_duplicate_and_mount_overrides(tmp_path: Path, addition: bytes) -> None:
    catalog, data = release_inputs(tmp_path)
    raw = (tmp_path / "judge.service").read_bytes() + addition
    (tmp_path / "judge.service").write_bytes(raw)
    data["units"]["judge"].update(size=len(raw), sha256=sha256(raw))
    catalog.write_bytes(canonical(data))
    with pytest.raises(ArtifactError, match="unit"):
        build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=tmp_path / "release.tar")


def test_shared_directory_writer_preserves_existing_modes_and_refuses_writable_parents(tmp_path: Path) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    existing = tmp_path / "protected"
    existing.mkdir()
    existing.chmod(0o750)
    writable = tmp_path / "writable"
    writable.mkdir()
    writable.chmod(0o777)
    try:
        child, made = _child_directory(parent, "protected", create=True)
        try:
            assert not made
            assert stat.S_IMODE(os.fstat(child).st_mode) == 0o750
        finally:
            os.close(child)
        with pytest.raises(ArtifactError, match="writable"):
            _child_directory(parent, "writable", create=True)
    finally:
        os.close(parent)
    assert stat.S_IMODE(writable.stat().st_mode) == 0o777


@pytest.mark.parametrize("mutation", ["entry", "root", "mode", "unit"])
def test_standalone_verifier_rejects_rebound_profile_mutations(tmp_path: Path, mutation: str) -> None:
    catalog, _ = release_inputs(tmp_path)
    output = tmp_path / "release.tar"
    build_release(catalog=catalog, expected_sha256=sha256(catalog.read_bytes()), input_root=tmp_path, output=output)
    files = read_archive(output.read_bytes())
    descriptor = json.loads(files.pop("release.json").data)
    prefix = ROLE_ROOTS["judge_supervisor"] + "/"
    module = prefix + "usr/lib/python3/dist-packages/elspeth_lints/core/judge_supervisor.py"
    if mutation == "entry":
        del files[module]
    elif mutation == "root":
        for name in list(files):
            if name.startswith(prefix + "usr/lib/python3/dist-packages/"):
                files[name.replace("usr/lib/python3/dist-packages/", "arbitrary/")] = files.pop(name)
    elif mutation == "mode":
        name = prefix + "usr/bin/python3"
        files[name] = ArtifactFile(files[name].data, 0o644)
    else:
        name = "etc/systemd/system/elspeth-judge-supervisor.service"
        files[name] = ArtifactFile(files[name].data + b" ExecStartPre=/usr/bin/unreviewed\n", 0o644)
    images = {}
    for role, root in ROLE_ROOTS.items():
        image = {name[len(root) + 1 :]: file for name, file in files.items() if name.startswith(root + "/")}
        manifest = json.loads(image["runtime-manifest.json"].data)
        manifest["files"] = {name: sha256(file.data) for name, file in image.items() if name != "runtime-manifest.json"}
        image["runtime-manifest.json"] = ArtifactFile(canonical(manifest), 0o644)
        files[root + "/runtime-manifest.json"] = image["runtime-manifest.json"]
        images[role] = image
    units = {role: files[f"etc/systemd/system/elspeth-{role}-supervisor.service"].data for role in ("judge", "baseline")}
    files.update(_configurations(images, units, descriptor["principals"]))
    descriptor["files"] = {name: {"sha256": sha256(file.data), "mode": file.mode} for name, file in files.items()}
    descriptor["installation"] = {name: name + ".pending" if name.startswith("etc/") else name for name in files}
    files["release.json"] = ArtifactFile(canonical(descriptor), 0o644)
    output.write_bytes(write_archive(files))
    with pytest.raises(ArtifactError):
        verify_release(output, expected_sha256=sha256(output.read_bytes()))
