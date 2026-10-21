"""Baseline publication never treats candidate execution as signed verification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from elspeth_lints.core import baseline_generation, operator_runtime

_PRODUCTION_CUSTODY = baseline_generation.require_publication_custody
_PRODUCTION_RUNTIME_INPUTS = baseline_generation.require_runtime_inputs


@pytest.fixture
def baseline_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name, text in {
        "src/elspeth/__init__.py": "value = 1\n",
        "config/cicd/enforce_tier_model/_defaults.yaml": "version: 1\n",
        "tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json": "{}\n",
        "tests/unit/elspeth_lints/test_allowlist_loader_unification.py": "# candidate code\n",
        "pyproject.toml": "[project]\nname = 'baseline-fixture'\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["/usr/bin/git", *args], cwd=root, check=True)
    private = tmp_path / "private"
    private.mkdir()
    evidence = operator_runtime.OperatorRuntimeEvidence(
        artifact_root=private,
        artifact_id="a" * 64,
        operator_uid=2001,
        candidate_uid=2002,
        invocation_cwd=root,
        argv=("regenerate-fingerprint-baseline",),
        python_minor=13,
    )
    monkeypatch.setattr(operator_runtime, "require_operator_runtime", lambda: evidence)
    monkeypatch.setattr(operator_runtime, "runtime_private_directory", lambda: private)
    # Most workflow tests model an already externally frozen checkout. Separate
    # admission tests below exercise the production ownership refusal itself.
    monkeypatch.setattr(baseline_generation, "require_publication_custody", lambda _root: None)
    monkeypatch.setattr(baseline_generation, "require_runtime_inputs", lambda _frozen: None)
    return root


def _accept_scans(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def signed(root: Path) -> None:
        calls.append("signed")
        assert (root / "src/elspeth/__init__.py").read_text() == "value = 1\n"

    monkeypatch.setattr(baseline_generation, "verify_signed_gate", signed)
    monkeypatch.setattr(baseline_generation, "capture_fingerprints", lambda _root: {"test.rule": []})


def test_verified_generation_compares_worker_before_only_baseline_publication(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _accept_scans(monkeypatch, calls)
    before = (baseline_repo / "src/elspeth/__init__.py").read_bytes()

    def worker(*, files: list[dict[str, str]], snapshot_sha256: str, expected_baseline: dict[str, list[dict[str, str]]]) -> None:
        calls.append("worker")
        assert len(snapshot_sha256) == 64
        assert expected_baseline == {"test.rule": []}
        assert {row["path"] for row in files} >= {"src/elspeth/__init__.py", "pyproject.toml"}

    monkeypatch.setattr(baseline_generation, "run_capture", worker)
    baseline_generation.regenerate(baseline_repo)
    assert calls == ["signed", "worker", "signed"]
    assert (baseline_repo / baseline_generation.BASELINE_PATH).read_text() == '{\n  "test.rule": []\n}\n'
    assert (baseline_repo / "src/elspeth/__init__.py").read_bytes() == before


@pytest.mark.parametrize("point", ["signed", "worker", "last-signed"])
def test_refusal_never_mutates_baseline(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch, point: str) -> None:
    original = (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes()
    calls: list[str] = []
    _accept_scans(monkeypatch, calls)

    def signed(_root: Path) -> None:
        calls.append("signed")
        if point == "signed" or (point == "last-signed" and len(calls) > 1):
            raise baseline_generation.BaselineGenerationError("signed gate refused")

    def worker(**_kwargs: object) -> None:
        calls.append("worker")
        if point == "worker":
            raise baseline_generation.BaselineGenerationError("worker refused")

    monkeypatch.setattr(baseline_generation, "verify_signed_gate", signed)
    monkeypatch.setattr(baseline_generation, "run_capture", worker)
    with pytest.raises(baseline_generation.BaselineGenerationError):
        baseline_generation.regenerate(baseline_repo)
    assert (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes() == original
    assert sorted(p.name for p in (baseline_repo / baseline_generation.BASELINE_PATH).parent.iterdir()) == ["fingerprint_baseline.json"]


@pytest.mark.parametrize("changed", ["src/elspeth/__init__.py", "pyproject.toml", ".github/workflows/example.yaml"])
def test_changed_or_added_input_after_worker_refuses_without_publication(
    baseline_repo: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    _accept_scans(monkeypatch, [])
    original = (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes()

    def worker(**_kwargs: object) -> None:
        target = baseline_repo / changed
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")

    monkeypatch.setattr(baseline_generation, "run_capture", worker)
    with pytest.raises(baseline_generation.BaselineGenerationError, match=r"changed|untracked"):
        baseline_generation.regenerate(baseline_repo)
    assert (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes() == original


def test_source_module_is_only_parsed_and_transferred_not_imported(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = baseline_repo / "candidate-ran"
    module = baseline_repo / "tests/unit/elspeth_lints/test_allowlist_loader_unification.py"
    module.write_text(f"raise AssertionError({str(marker)!r})\n")
    _accept_scans(monkeypatch, [])
    monkeypatch.setattr(baseline_generation, "run_capture", lambda **_kwargs: None)
    baseline_generation.regenerate(baseline_repo)
    assert not marker.exists()


def test_unadmitted_runtime_refuses_before_snapshot_or_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise operator_runtime.OperatorRuntimeUnavailableError("unadmitted")

    monkeypatch.setattr(operator_runtime, "require_operator_runtime", refuse)
    with pytest.raises(operator_runtime.OperatorRuntimeUnavailableError):
        baseline_generation.regenerate(tmp_path)


def test_signed_gate_forces_required_mode_and_rejects_empty_or_failed_output(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from elspeth_lints.core import cli

    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE", "shape-only-when-key-missing")
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "synthetic-test-key")

    def scan(argv: list[str]) -> int:
        assert os.environ["ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"] == "required"
        assert Path.cwd() == baseline_repo
        assert "trust_tier.tier_model" in argv
        return 0  # A successful code without an actual JSON result is not clearance.

    monkeypatch.setattr(cli, "main", scan)
    with pytest.raises(baseline_generation.BaselineGenerationError):
        baseline_generation.verify_signed_gate(baseline_repo)
    assert os.environ["ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"] == "shape-only-when-key-missing"


def test_publication_preserves_existing_baseline_mode(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import stat

    target = baseline_repo / baseline_generation.BASELINE_PATH
    target.chmod(0o640)
    _accept_scans(monkeypatch, [])
    monkeypatch.setattr(baseline_generation, "run_capture", lambda **_kwargs: None)
    baseline_generation.regenerate(baseline_repo)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_index_change_refuses_after_capture(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _accept_scans(monkeypatch, [])
    original = (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes()

    def worker(**_kwargs: object) -> None:
        subprocess.run(["/usr/bin/git", "update-index", "--chmod=+x", "src/elspeth/__init__.py"], cwd=baseline_repo, check=True)

    monkeypatch.setattr(baseline_generation, "run_capture", worker)
    with pytest.raises(baseline_generation.BaselineGenerationError, match="changed"):
        baseline_generation.regenerate(baseline_repo)
    assert (baseline_repo / baseline_generation.BASELINE_PATH).read_bytes() == original


def test_symlink_replacement_cannot_read_or_overwrite_private_target(
    baseline_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept_scans(monkeypatch, [])
    private = tmp_path / "private-target"
    private.write_text("private-original")
    target = baseline_repo / baseline_generation.BASELINE_PATH

    def worker(**_kwargs: object) -> None:
        target.unlink()
        target.symlink_to(private)

    monkeypatch.setattr(baseline_generation, "run_capture", worker)
    with pytest.raises(baseline_generation.BaselineGenerationError):
        baseline_generation.regenerate(baseline_repo)
    assert target.is_symlink()
    assert private.read_text() == "private-original"


def test_protected_cli_refuses_before_env_file_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from elspeth_lints.core import cli

    def refuse() -> None:
        raise operator_runtime.OperatorRuntimeUnavailableError("unadmitted")

    monkeypatch.setattr(operator_runtime, "require_operator_runtime", refuse)
    assert cli.main(["regenerate-fingerprint-baseline", "--env-file", str(tmp_path / "absent.env")]) == 2


def test_protected_cli_refuses_commit_before_env_file_read(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth_lints.core import cli

    def forbidden(_file: Path | None) -> None:
        raise AssertionError("must not load an environment file for unsupported Git mutation")

    monkeypatch.setattr(cli, "_load_judge_signing_env_file", forbidden)
    assert cli.main(["regenerate-fingerprint-baseline", "--commit", "--env-file", "absent.env"]) == 2


def test_actual_installed_signed_gate_accepts_clean_and_refuses_violation(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth_lints.rules.trust_tier.tier_model import rule as tier_rule

    # This tiny source fixture has none of the application's named boundaries.
    # Keep the real scanner/signature route and remove only that unrelated map.
    monkeypatch.setattr(tier_rule, "resolve_named_boundary_contexts", lambda _root: [])
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "synthetic-test-key")
    (baseline_repo / "config/cicd/enforce_tier_model/_defaults.yaml").write_text("allow_hits: []\n")
    baseline_generation.verify_signed_gate(baseline_repo)
    (baseline_repo / "src/elspeth/__init__.py").write_text("value = getattr(object(), 'missing', None)\n")
    with pytest.raises(baseline_generation.BaselineGenerationError, match="signed enforce gate refused"):
        baseline_generation.verify_signed_gate(baseline_repo)


def test_actual_signed_gate_rejects_tampered_metadata(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    from tests.unit.elspeth_lints.test_allowlist_judge_metadata_integrity import (
        _TEST_JUDGE_METADATA_HMAC_KEY,
        _write_judge_gated_yaml,
    )

    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", _TEST_JUDGE_METADATA_HMAC_KEY)
    directory = baseline_repo / "config/cicd/enforce_tier_model"
    (directory / "_defaults.yaml").write_text("allow_hits: []\n")
    path = directory / "sample.yaml"
    _write_judge_gated_yaml(
        yaml_path=path,
        key="__init__.py:R3:<module>:fp=1",
        file_fingerprint=hashlib.sha256((baseline_repo / "src/elspeth/__init__.py").read_bytes()).hexdigest(),
        ast_path="body[0]",
    )
    path.write_text(path.read_text().replace("judge accepted the suppression", "tampered rationale"))
    with pytest.raises(baseline_generation.BaselineGenerationError) as refused:
        baseline_generation.verify_signed_gate(baseline_repo)
    assert "signature" in str(refused.value.__cause__).lower()


def test_snapshot_never_forwards_selected_key_bytes(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_key = "synthetic-private-baseline-value"
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", synthetic_key)
    (baseline_repo / "src/elspeth/__init__.py").write_text(repr(synthetic_key))
    with pytest.raises(baseline_generation.BaselineGenerationError, match="selected signing value"):
        baseline_generation.freeze_inputs(baseline_repo)


def test_candidate_writable_checkout_refuses_publication_custody(baseline_repo: Path) -> None:
    with pytest.raises(baseline_generation.BaselineGenerationError, match="custody"):
        _PRODUCTION_CUSTODY(baseline_repo)


def test_synthetic_protected_checkout_passes_custody_policy(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    real_lstat = Path.lstat

    def protected_metadata(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        values = list(metadata)
        values[0] &= ~0o022
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", protected_metadata)
    _PRODUCTION_CUSTODY(baseline_repo)


def test_note_observations_remain_in_capture_and_do_not_turn_gate_red(baseline_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from elspeth_lints.core import cli

    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "synthetic-test-key")

    def scan(_argv: list[str]) -> int:
        print(json.dumps([{"file_path": "__init__.py", "fingerprint": "note1", "severity": "note"}]))
        return 0

    monkeypatch.setattr(cli, "main", scan)
    baseline_generation.verify_signed_gate(baseline_repo)
    monkeypatch.setattr(baseline_generation, "RULE_ROOTS", (("trust_tier.tier_model", "src/elspeth"),))
    assert baseline_generation.capture_fingerprints(baseline_repo) == {
        "trust_tier.tier_model": [{"file_path": "__init__.py", "fingerprint": "note1"}]
    }


def test_first_party_runtime_must_match_frozen_package_census(baseline_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "__init__.py").write_text("value = 1\n")
    monkeypatch.setattr(baseline_generation, "runtime_packages", lambda: ((installed, "src/elspeth", True),))
    frozen = baseline_generation.freeze_inputs(baseline_repo)
    _PRODUCTION_RUNTIME_INPUTS(frozen)
    (baseline_repo / "src/elspeth/__init__.py").write_text("value = 2\n")
    with pytest.raises(baseline_generation.BaselineGenerationError, match="runtime differs"):
        _PRODUCTION_RUNTIME_INPUTS(baseline_generation.freeze_inputs(baseline_repo))
    (installed / "extra_plugin.py").write_text("value = 2\n")
    with pytest.raises(baseline_generation.BaselineGenerationError, match="runtime differs"):
        _PRODUCTION_RUNTIME_INPUTS(frozen)
