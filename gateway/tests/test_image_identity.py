"""Offline image identity used by pre-secret deployment admission."""

import hashlib
import importlib.metadata
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import elspeth_llm_gateway.image_identity as image_identity
import pytest
from elspeth_llm_gateway.core.adapter_identity import compute_adapter_fingerprint, resolve_adapter
from elspeth_llm_gateway.core.config import ConfigError
from elspeth_llm_gateway.image_identity import collect_image_identity, main
from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor
from elspeth_llm_gateway.sdk.types import Capability

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def _expected_package_fingerprint(package_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        relative = path.relative_to(package_root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        name="agency_adapter",
        version="1.2.3",
        adapter_api_major=1,
        capabilities=frozenset({Capability.TEXT}),
    )


def test_reference_image_identity_matches_the_installed_runtime_and_adapter() -> None:
    identity = collect_image_identity("reference_v1_invoke")
    adapter_package = GATEWAY_ROOT / "src" / "elspeth_llm_gateway" / "reference"
    expected_fingerprint = _expected_package_fingerprint(adapter_package)

    assert identity == {
        "schema": "elspeth.llm-gateway.image-identity.v1",
        "runtime_identity": "elspeth-llm-gateway",
        "runtime_version": "0.1.0",
        "contract_major": 1,
        "adapter_name": "reference_v1_invoke",
        "adapter_version": "0.1.0",
        "adapter_api_major": 1,
        "adapter_fingerprint": expected_fingerprint,
    }


def test_image_identity_cli_emits_only_the_bounded_canonical_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--adapter", "reference_v1_invoke"]) == 0

    output = capsys.readouterr().out
    assert output.endswith("\n")
    assert output == json.dumps(json.loads(output), sort_keys=True, separators=(",", ":")) + "\n"
    assert set(json.loads(output)) == {
        "schema",
        "runtime_identity",
        "runtime_version",
        "contract_major",
        "adapter_name",
        "adapter_version",
        "adapter_api_major",
        "adapter_fingerprint",
    }


def test_image_identity_cli_never_reads_or_emits_runtime_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bearer = "REDACTED_BEARER_VALUE"
    oauth_secret = "REDACTED_OAUTH_VALUE"
    monkeypatch.setenv("ELSPETH_LLM_GATEWAY_INBOUND_BEARER", bearer)
    monkeypatch.setenv("ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET", oauth_secret)

    assert main(["--adapter", "reference_v1_invoke"]) == 0

    output = capsys.readouterr().out
    assert bearer not in output
    assert oauth_secret not in output


def test_adapter_fingerprint_covers_every_file_in_the_installed_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "agency_adapter"
    package.mkdir()
    (package / "__init__.py").write_text("")
    adapter_source = package / "adapter.py"
    adapter_source.write_text("from .request import build_request\n")
    delegated_source = package / "request.py"
    delegated_source.write_text("def build_request(): return {'version': 1}\n")
    monkeypatch.setattr(inspect, "getsourcefile", lambda _adapter_type: str(adapter_source))

    before = compute_adapter_fingerprint(object())
    expected_before = _expected_package_fingerprint(package)
    delegated_source.write_text("def build_request(): return {'version': 2}\n")
    after = compute_adapter_fingerprint(object())

    assert before == expected_before
    assert after == _expected_package_fingerprint(package)
    assert before != after
    assert len(before) == 64


def test_adapter_fingerprint_rejects_symlinked_package_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "agency_adapter"
    package.mkdir()
    (package / "__init__.py").write_text("")
    adapter_source = package / "adapter.py"
    adapter_source.write_text("class Adapter: pass\n")
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 'not package-owned'\n")
    (package / "delegated.py").symlink_to(outside)
    monkeypatch.setattr(inspect, "getsourcefile", lambda _adapter_type: str(adapter_source))

    with pytest.raises(RuntimeError, match="adapter package contains a symlink"):
        compute_adapter_fingerprint(object())


def test_installed_adapter_fingerprint_covers_distribution_files_and_entry_point_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "agency_adapter"
    nested = package / "impl"
    metadata = tmp_path / "agency_adapter-1.2.3.dist-info"
    nested.mkdir(parents=True)
    metadata.mkdir()
    files = [
        Path("agency_adapter/__init__.py"),
        Path("agency_adapter/shared.py"),
        Path("agency_adapter/impl/__init__.py"),
        Path("agency_adapter/impl/adapter.py"),
        Path("agency_adapter-1.2.3.dist-info/entry_points.txt"),
    ]
    for relative in files:
        path = tmp_path / relative
        path.write_text(f"content:{relative.as_posix()}\n")

    distribution = SimpleNamespace(files=files, locate_file=lambda path: tmp_path / path)
    entry_point = SimpleNamespace(name="agency_adapter", dist=distribution)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_kwargs: [entry_point])

    before = compute_adapter_fingerprint(object(), adapter_name="agency_adapter", require_package=True)
    (package / "shared.py").write_text("changed delegated package code\n")
    after_code_change = compute_adapter_fingerprint(object(), adapter_name="agency_adapter", require_package=True)
    (metadata / "entry_points.txt").write_text("changed entry point target\n")
    after_entry_point_change = compute_adapter_fingerprint(object(), adapter_name="agency_adapter", require_package=True)

    assert before != after_code_change
    assert after_code_change != after_entry_point_change
    assert all(len(value) == 64 for value in (before, after_code_change, after_entry_point_change))


def test_adapter_resolution_rejects_duplicate_entry_point_names(monkeypatch: pytest.MonkeyPatch) -> None:
    matches = [SimpleNamespace(name="agency_adapter"), SimpleNamespace(name="agency_adapter")]
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_kwargs: matches)

    with pytest.raises(ConfigError, match="ambiguous_adapter:agency_adapter"):
        resolve_adapter("agency_adapter")


def test_image_identity_cli_sanitizes_adapter_failures(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "REDACTED_ADAPTER_ERROR_VALUE"

    def fail_resolution(_adapter_name: str) -> object:
        sys.stdout.write(secret + "\n")
        raise RuntimeError(secret)

    monkeypatch.setattr(image_identity, "resolve_adapter", fail_resolution)

    assert main(["--adapter", "broken_adapter"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "gateway_image_identity_unavailable\n"
    assert secret not in captured.err


def test_image_identity_cli_suppresses_adapter_output_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    secret = "REDACTED_ADAPTER_OUTPUT_VALUE"

    class NoisyAdapter:
        def descriptor(self) -> AdapterDescriptor:
            os.write(1, (secret + "-stdout\n").encode())
            os.write(2, (secret + "-stderr\n").encode())
            return _descriptor()

    monkeypatch.setattr(image_identity, "resolve_adapter", lambda _name: NoisyAdapter())
    monkeypatch.setattr(image_identity, "compute_adapter_fingerprint", lambda _adapter, **_kwargs: "a" * 64)

    assert main(["--adapter", "agency_adapter"]) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    assert secret not in captured.out
    assert json.loads(captured.out)["adapter_name"] == "agency_adapter"


def test_image_identity_cli_rejects_non_owned_descriptor_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = SimpleNamespace(descriptor=lambda: {"name": "masquerader"})
    monkeypatch.setattr(image_identity, "resolve_adapter", lambda _name: adapter)
    monkeypatch.setattr(image_identity, "compute_adapter_fingerprint", lambda _adapter, **_kwargs: "a" * 64)

    assert main(["--adapter", "agency_adapter"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "gateway_image_identity_unavailable\n"


def test_image_identity_cli_sanitizes_adapter_system_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "REDACTED-system-exit-value"

    class ExitingAdapter:
        def descriptor(self) -> AdapterDescriptor:
            raise SystemExit(secret)

    monkeypatch.setattr(image_identity, "resolve_adapter", lambda _name: ExitingAdapter())

    assert main(["--adapter", "agency_adapter"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "gateway_image_identity_unavailable\n"
    assert secret not in captured.err


def test_image_identity_cli_rejects_an_unavailable_adapter_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(image_identity, "compute_adapter_fingerprint", lambda _adapter, **_kwargs: "0" * 64)

    assert main(["--adapter", "reference_v1_invoke"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "gateway_image_identity_unavailable\n"


def test_reference_dockerfile_stamps_the_code_owned_image_identity() -> None:
    dockerfile = (GATEWAY_ROOT / "Dockerfile").read_text()
    reference_identity = collect_image_identity("reference_v1_invoke")

    assert "ARG GATEWAY_REVISION" in dockerfile
    assert 'test -n "$GATEWAY_REVISION"' in dockerfile
    assert 'org.opencontainers.image.revision="${GATEWAY_REVISION}"' in dockerfile
    for field, label in {
        "runtime_identity": "io.elspeth.llm-gateway.runtime-identity",
        "runtime_version": "io.elspeth.llm-gateway.runtime-version",
        "contract_major": "io.elspeth.llm-gateway.contract-major",
        "adapter_name": "io.elspeth.llm-gateway.adapter-name",
        "adapter_version": "io.elspeth.llm-gateway.adapter-version",
        "adapter_api_major": "io.elspeth.llm-gateway.adapter-api-major",
        "adapter_fingerprint": "io.elspeth.llm-gateway.adapter-fingerprint",
    }.items():
        assert f'{label}="{reference_identity[field]}"' in dockerfile


def test_derived_image_instructions_require_overriding_every_adapter_identity_label() -> None:
    readme = (GATEWAY_ROOT / "README.md").read_text()
    derived_images = readme[readme.index("### Derived images") :]

    for label in (
        "org.opencontainers.image.revision",
        "io.elspeth.llm-gateway.adapter-name",
        "io.elspeth.llm-gateway.adapter-version",
        "io.elspeth.llm-gateway.adapter-api-major",
        "io.elspeth.llm-gateway.adapter-fingerprint",
    ):
        assert label in derived_images
    assert "python -m elspeth_llm_gateway.image_identity" in derived_images
    assert "REPLACE_WITH_64_LOWERCASE_HEX" in derived_images


def test_derived_image_installs_as_root_then_restores_the_runtime_uid() -> None:
    readme = (GATEWAY_ROOT / "README.md").read_text()
    derived_images = readme[readme.index("### Derived images") :]

    assert "USER root" in derived_images
    assert "USER 65532:65532" in derived_images
    root = derived_images.index("USER root")
    install = derived_images.index("/venv/bin/pip install")
    runtime_user = derived_images.index("USER 65532:65532")
    assert root < install < runtime_user


def test_derived_image_installs_an_exact_hash_verified_adapter_artifact() -> None:
    readme = (GATEWAY_ROOT / "README.md").read_text()
    derived_images = readme[readme.index("### Derived images") :]

    assert "ADAPTER_WHEEL_SHA256" in derived_images
    assert "sha256sum -c -" in derived_images
    assert "--no-deps /build/yourorg_adapter-X.Y.Z-py3-none-any.whl" in derived_images
    assert "yourorg-adapter==X.Y.Z" not in derived_images


def test_derived_image_documents_provisional_then_final_fingerprint_verification() -> None:
    readme = (GATEWAY_ROOT / "README.md").read_text()
    derived_images = " ".join(readme[readme.index("### Derived images") :].lower().split())

    assert "provisional local image" in derived_images
    assert "never sign or publish" in derived_images
    assert "final image" in derived_images
    assert "re-run the offline command" in derived_images
