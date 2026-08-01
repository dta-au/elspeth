"""Deterministic reconciliation tests for the trust-tier remediation corpus."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from elspeth_lints.core import tier_model_corpus as corpus_module
from elspeth_lints.core.allowlist import _JUDGE_METADATA_SIGNATURE_ENV_VAR
from elspeth_lints.core.atomic_io import AtomicWriteConflictError
from elspeth_lints.core.review_bundle import (
    SCHEMA_VERSION,
    BundleAction,
    ReviewBundle,
    write_bundle,
)
from elspeth_lints.core.tier_model_corpus import (
    CLASSIFICATION_CORPUS_SCHEMA_VERSION,
    ClassificationCorpus,
    CorpusReconciliationError,
    CorpusStaleError,
    action_scan_sha256,
    create_classification_corpus,
    dump_classification_corpus,
    load_classification_corpus,
    source_snapshot_sha256,
    verify_classification_corpus,
)
from elspeth_lints.core.tier_model_corpus import (
    main as corpus_main,
)
from elspeth_lints.mcp import server as judge_server

_HEAD = "1" * 40
_SOURCE_SHA256 = "2" * 64


def _justify(key: str, *, file_path: str = "plugins/widget.py") -> BundleAction:
    return BundleAction(
        lane="new_judgment",
        kind="justify",
        key=key,
        file_path=file_path,
        symbol="Widget.lookup",
        rule="R1",
        fingerprint=key.rsplit("=", 1)[1],
        scope_fingerprint="a" * 64,
        ast_path="body[0]/body[0]/value",
    )


def _drift(key: str) -> BundleAction:
    return BundleAction(
        lane="resign",
        kind="drift_repair",
        key=key,
        diagnosis_status="SCOPE_BINDING_DRIFT",
        source_file="plugins.yaml",
    )


def _bundle(actions: tuple[BundleAction, ...]) -> ReviewBundle:
    return ReviewBundle(
        bundle_id="release-072",
        schema_version=SCHEMA_VERSION,
        created_at="2026-07-28T00:00:00+00:00",
        staged_by="codex-tier-analyzer",
        root="src/elspeth",
        allowlist_dir="config/cicd/enforce_tier_model",
        source_rev=_HEAD,
        source_dirty=False,
        actions=actions,
    )


def _write_bundle(tmp_path: Path, actions: tuple[BundleAction, ...]) -> Path:
    return write_bundle(_bundle(actions), staged_dir=tmp_path / "staged")


def _create(
    tmp_path: Path,
    actions: tuple[BundleAction, ...],
    *,
    live_actions: tuple[BundleAction, ...] | None = None,
) -> tuple[Path, ClassificationCorpus]:
    bundle_path = _write_bundle(tmp_path, actions)
    if live_actions is None:
        live_actions = actions
    corpus = create_classification_corpus(
        bundle_path=bundle_path,
        git_head=_HEAD,
        source_snapshot_sha256_value=_SOURCE_SHA256,
        live_actions=live_actions,
    )
    return bundle_path, corpus


def _git(*args: str, cwd: Path) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


def _real_staged_repo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    root = repo / "src/elspeth"
    allowlist_dir = repo / "config/cicd/enforce_tier_model"
    staged_dir = repo / ".elspeth/staged-reviews"
    (root / "plugins").mkdir(parents=True)
    allowlist_dir.mkdir(parents=True)
    (repo / ".gitignore").write_text(".elspeth/*\n", encoding="utf-8")
    (allowlist_dir / "_defaults.yaml").write_text(
        "version: 1\ndefaults:\n  fail_on_stale: false\n  fail_on_expired: false\n",
        encoding="utf-8",
    )
    (root / "plugins/widget.py").write_text(
        "def lookup(payload):\n    return payload.get('name', 'anonymous')\n",
        encoding="utf-8",
    )
    _git("init", cwd=repo)
    _git("config", "user.name", "Tier Corpus Test", cwd=repo)
    _git("config", "user.email", "tier-corpus@example.invalid", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "fixture", cwd=repo)

    ctx = judge_server._ServerContext(root=root, allowlist_dir=allowlist_dir, staged_dir=staged_dir)
    outcome = judge_server._run_tool(
        ctx,
        "stage_scan",
        {
            "bundle_id": "real-stage-scan",
            "staged_by": "tier-corpus-test",
        },
    )
    if outcome.is_error:
        raise AssertionError(outcome.text)
    payload = json.loads(outcome.text)
    bundle_path = Path(payload["written_path"])
    return repo, root, allowlist_dir, bundle_path


def test_create_corpus_binds_digests_and_leaves_provenance_manual(tmp_path: Path) -> None:
    actions = (
        _justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),
        _drift("plugins/drift.py:R5:Drift:load:fp=bbbb"),
    )
    bundle_path, corpus = _create(tmp_path, actions)

    assert corpus.schema_version == CLASSIFICATION_CORPUS_SCHEMA_VERSION
    assert corpus.bundle_id == "release-072"
    assert corpus.bundle_sha256 != ""
    assert corpus.git_head == _HEAD
    assert corpus.source_snapshot_sha256 == _SOURCE_SHA256
    assert corpus.live_scan_sha256 == action_scan_sha256(actions)
    assert [entry.action_key for entry in corpus.entries] == sorted(action.key for action in actions)
    assert [entry.action_kind for entry in corpus.entries] == ["drift_repair", "justify"]
    assert all(entry.classification is None for entry in corpus.entries)
    assert all(entry.producer_provenance is None for entry in corpus.entries)
    text = dump_classification_corpus(corpus)
    assert bundle_path.read_text(encoding="utf-8") not in text
    assert load_classification_corpus(text) == corpus


def test_action_scan_identity_is_order_independent_but_field_sensitive() -> None:
    first = _justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa")
    second = _drift("plugins/drift.py:R5:Drift:load:fp=bbbb")

    assert action_scan_sha256((first, second)) == action_scan_sha256((second, first))
    changed = BundleAction(
        lane=first.lane,
        kind=first.kind,
        key=first.key,
        file_path=first.file_path,
        symbol=first.symbol,
        rule="R5",
        fingerprint=first.fingerprint,
        scope_fingerprint=first.scope_fingerprint,
        ast_path=first.ast_path,
    )
    assert action_scan_sha256((first, second)) != action_scan_sha256((changed, second))


def test_action_scan_rejects_cross_kind_same_key() -> None:
    key = "plugins/widget.py:R1:Widget:lookup:fp=aaaa"
    justify = _justify(key)
    drift = _drift(key)

    with pytest.raises(CorpusReconciliationError, match="duplicate action identity"):
        action_scan_sha256((justify, drift))


def test_create_rejects_stale_bundle_against_independent_live_actions(tmp_path: Path) -> None:
    staged = (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),)
    live = (_justify("plugins/widget.py:R1:Widget:lookup:fp=bbbb"),)
    bundle_path = _write_bundle(tmp_path, staged)

    with pytest.raises(CorpusStaleError, match="bundle actions do not match the live scan"):
        create_classification_corpus(
            bundle_path=bundle_path,
            git_head=_HEAD,
            source_snapshot_sha256_value=_SOURCE_SHA256,
            live_actions=live,
        )


def test_load_rejects_unknown_schema_and_duplicate_rows(tmp_path: Path) -> None:
    _, corpus = _create(tmp_path, (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),))
    payload = json.loads(dump_classification_corpus(corpus))
    payload["schema_version"] = CLASSIFICATION_CORPUS_SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="schema_version"):
        load_classification_corpus(json.dumps(payload))

    payload["schema_version"] = CLASSIFICATION_CORPUS_SCHEMA_VERSION
    payload["entries"].append(payload["entries"][0])
    with pytest.raises(CorpusReconciliationError, match="duplicate action row"):
        load_classification_corpus(json.dumps(payload))


@pytest.mark.parametrize("mutation", ("missing", "extra", "rewritten_key"))
def test_verify_rejects_missing_extra_or_rewritten_rows(tmp_path: Path, mutation: str) -> None:
    actions = (
        _justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),
        _drift("plugins/drift.py:R5:Drift:load:fp=bbbb"),
    )
    bundle_path, corpus = _create(tmp_path, actions)
    payload = json.loads(dump_classification_corpus(corpus))
    if mutation == "missing":
        payload["entries"].pop()
    elif mutation == "extra":
        extra = dict(payload["entries"][0])
        extra["action_key"] = "plugins/extra.py:R1:Extra:load:fp=cccc"
        payload["entries"].append(extra)
    else:
        payload["entries"][0]["action_key"] = actions[0].key
        payload["entries"][1]["action_key"] = actions[1].key

    edited = load_classification_corpus(json.dumps(payload))
    with pytest.raises(CorpusReconciliationError):
        verify_classification_corpus(
            corpus=edited,
            bundle_path=bundle_path,
            git_head=_HEAD,
            source_snapshot_sha256_value=_SOURCE_SHA256,
            live_actions=actions,
        )


@pytest.mark.parametrize("stale_surface", ("bundle", "head", "source", "scan"))
def test_verify_rejects_each_stale_identity_surface(tmp_path: Path, stale_surface: str) -> None:
    actions = (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),)
    bundle_path, corpus = _create(tmp_path, actions)
    git_head = _HEAD
    source_sha256 = _SOURCE_SHA256
    live_actions = actions
    if stale_surface == "bundle":
        bundle_path.write_text(bundle_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif stale_surface == "head":
        git_head = "3" * 40
    elif stale_surface == "source":
        source_sha256 = "4" * 64
    else:
        live_actions = (_justify("plugins/widget.py:R1:Widget:lookup:fp=bbbb"),)

    with pytest.raises(CorpusStaleError):
        verify_classification_corpus(
            corpus=corpus,
            bundle_path=bundle_path,
            git_head=git_head,
            source_snapshot_sha256_value=source_sha256,
            live_actions=live_actions,
        )


def test_verify_rejects_edited_structural_facts_even_when_row_key_is_live(tmp_path: Path) -> None:
    actions = (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),)
    bundle_path, corpus = _create(tmp_path, actions)
    payload = json.loads(dump_classification_corpus(corpus))
    payload["entries"][0]["rule"] = "R5"
    edited = load_classification_corpus(json.dumps(payload))

    with pytest.raises(CorpusReconciliationError, match="structural facts"):
        verify_classification_corpus(
            corpus=edited,
            bundle_path=bundle_path,
            git_head=_HEAD,
            source_snapshot_sha256_value=_SOURCE_SHA256,
            live_actions=actions,
        )


def test_verify_can_require_every_manual_classification(tmp_path: Path) -> None:
    actions = (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),)
    bundle_path, corpus = _create(tmp_path, actions)

    with pytest.raises(CorpusReconciliationError, match="unclassified"):
        verify_classification_corpus(
            corpus=corpus,
            bundle_path=bundle_path,
            git_head=_HEAD,
            source_snapshot_sha256_value=_SOURCE_SHA256,
            live_actions=actions,
            require_classified=True,
        )

    payload = json.loads(dump_classification_corpus(corpus))
    payload["entries"][0]["classification"] = "repair"
    payload["entries"][0]["producer_provenance"] = "Tier 1 internal state"
    classified = load_classification_corpus(json.dumps(payload))
    summary = verify_classification_corpus(
        corpus=classified,
        bundle_path=bundle_path,
        git_head=_HEAD,
        source_snapshot_sha256_value=_SOURCE_SHA256,
        live_actions=actions,
        require_classified=True,
    )

    assert summary.actions_total == 1
    assert summary.drift_actions_total == 0
    assert summary.classified_total == 1


def test_verify_requires_manual_producer_provenance_for_completed_rows(tmp_path: Path) -> None:
    actions = (_justify("plugins/widget.py:R1:Widget:lookup:fp=aaaa"),)
    bundle_path, corpus = _create(tmp_path, actions)
    payload = json.loads(dump_classification_corpus(corpus))
    payload["entries"][0]["classification"] = "repair"
    missing_provenance = load_classification_corpus(json.dumps(payload))

    with pytest.raises(CorpusReconciliationError, match="producer provenance"):
        verify_classification_corpus(
            corpus=missing_provenance,
            bundle_path=bundle_path,
            git_head=_HEAD,
            source_snapshot_sha256_value=_SOURCE_SHA256,
            live_actions=actions,
            require_classified=True,
        )


def test_source_snapshot_digest_covers_source_and_allowlist_bytes(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    allowlist_dir = tmp_path / "allowlist"
    source_root.mkdir()
    allowlist_dir.mkdir()
    source = source_root / "widget.py"
    allowlist = allowlist_dir / "widget.yaml"
    source.write_text("value = 1\n", encoding="utf-8")
    allowlist.write_text("allow_hits: []\n", encoding="utf-8")

    initial = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)
    source.write_text("value = 2\n", encoding="utf-8")
    source_changed = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)
    allowlist.write_text("allow_hits:\n", encoding="utf-8")
    allowlist_changed = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)

    assert len({initial, source_changed, allowlist_changed}) == 3


def test_source_snapshot_uses_the_tier_scanner_file_predicate(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    allowlist_dir = tmp_path / "allowlist"
    source_root.mkdir()
    allowlist_dir.mkdir()
    (source_root / "widget.py").write_text("value = 1\n", encoding="utf-8")
    (allowlist_dir / "widget.yaml").write_text("allow_hits: []\n", encoding="utf-8")
    initial = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)

    node_modules = source_root / "node_modules"
    pycache = source_root / "__pycache__"
    node_modules.mkdir()
    pycache.mkdir()
    (node_modules / "ignored.py").write_text("ignored = True\n", encoding="utf-8")
    (pycache / "widget.cpython-313.pyc").write_bytes(b"not Python source")
    assert source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir) == initial

    added = source_root / "added.py"
    added.write_text("added = 1\n", encoding="utf-8")
    after_add = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)
    added.write_text("added = 2\n", encoding="utf-8")
    after_change = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)
    added.unlink()
    after_delete = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)

    assert len({initial, after_add, after_change}) == 3
    assert after_delete == initial


def test_source_snapshot_delegates_python_discovery_to_tier_scanner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "src"
    allowlist_dir = tmp_path / "allowlist"
    source_root.mkdir()
    allowlist_dir.mkdir()
    included = source_root / "included.py"
    omitted = source_root / "omitted.py"
    included.write_text("included = 1\n", encoding="utf-8")
    omitted.write_text("omitted = 1\n", encoding="utf-8")
    (allowlist_dir / "_defaults.yaml").write_text("version: 1\n", encoding="utf-8")
    calls: list[Path] = []

    def fake_iter_scannable_python_files(root: Path, exclude_patterns: list[str] | None = None):
        calls.append(root)
        assert exclude_patterns is None
        yield included

    monkeypatch.setattr(corpus_module, "iter_scannable_python_files", fake_iter_scannable_python_files)
    initial = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)
    omitted.write_text("omitted = 2\n", encoding="utf-8")
    after_omitted_change = source_snapshot_sha256(source_root=source_root, allowlist_dir=allowlist_dir)

    assert calls == [source_root, source_root]
    assert after_omitted_change == initial


def test_real_stage_scan_actions_reconcile_without_a_mutable_report(tmp_path: Path) -> None:
    root = tmp_path / "src_root"
    allowlist_dir = tmp_path / "allowlist"
    staged_dir = tmp_path / "staged"
    (root / "plugins").mkdir(parents=True)
    allowlist_dir.mkdir()
    (allowlist_dir / "_defaults.yaml").write_text(
        "version: 1\ndefaults:\n  fail_on_stale: false\n  fail_on_expired: false\n",
        encoding="utf-8",
    )
    (root / "plugins/widget.py").write_text(
        "def lookup(payload):\n    return payload.get('name', 'anonymous')\n",
        encoding="utf-8",
    )
    ctx = judge_server._ServerContext(root=root, allowlist_dir=allowlist_dir, staged_dir=staged_dir)
    live_actions = tuple(judge_server._build_scan_actions(ctx))
    assert len(live_actions) == 1
    bundle_path = _write_bundle(tmp_path, live_actions)

    corpus = create_classification_corpus(
        bundle_path=bundle_path,
        git_head=_HEAD,
        source_snapshot_sha256_value=source_snapshot_sha256(source_root=root, allowlist_dir=allowlist_dir),
        live_actions=live_actions,
    )
    summary = verify_classification_corpus(
        corpus=corpus,
        bundle_path=bundle_path,
        git_head=_HEAD,
        source_snapshot_sha256_value=source_snapshot_sha256(source_root=root, allowlist_dir=allowlist_dir),
        live_actions=tuple(judge_server._build_scan_actions(ctx)),
    )

    assert summary.actions_total == 1
    assert corpus.entries[0].action_key == live_actions[0].key


def test_public_cli_creates_and_verifies_real_stage_scan_corpus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    common = [
        str(bundle_path),
        "--repo-root",
        str(repo),
        "--root",
        str(root),
        "--allowlist-dir",
        str(allowlist_dir),
    ]

    assert corpus_main(["create", *common, "--output", str(output)]) == 0
    create_payload = json.loads(capsys.readouterr().out)
    assert create_payload["actions_total"] == 1
    assert create_payload["report_path"] == str(output.resolve())
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    corpus = load_classification_corpus(output.read_text(encoding="utf-8"))
    assert len(corpus.entries) == 1
    assert corpus.entries[0].classification is None

    assert corpus_main(["verify", *common, "--corpus", str(output)]) == 0
    verify_payload = json.loads(capsys.readouterr().out)
    assert verify_payload == {
        "actions_total": 1,
        "classified_total": 0,
        "drift_actions_total": 0,
        "report_path": str(output.resolve()),
    }


def test_public_cli_creates_private_corpus_directory_under_permissive_umask(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    previous_umask = os.umask(0o022)
    try:
        result = corpus_main(
            [
                "create",
                str(bundle_path),
                "--repo-root",
                str(repo),
                "--root",
                str(root),
                "--allowlist-dir",
                str(allowlist_dir),
                "--output",
                str(output),
            ]
        )
    finally:
        os.umask(previous_umask)

    assert result == 0
    capsys.readouterr()
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_public_cli_require_classified_enforces_manual_completion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    common = [
        str(bundle_path),
        "--repo-root",
        str(repo),
        "--root",
        str(root),
        "--allowlist-dir",
        str(allowlist_dir),
    ]
    assert corpus_main(["create", *common, "--output", str(output)]) == 0
    capsys.readouterr()

    assert corpus_main(["verify", *common, "--corpus", str(output), "--require-classified"]) == 2
    assert "unclassified" in capsys.readouterr().err

    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["entries"][0]["classification"] = "repair"
    payload["entries"][0]["producer_provenance"] = "Tier 1 internal state"
    output.write_text(json.dumps(payload), encoding="utf-8")
    assert corpus_main(["verify", *common, "--corpus", str(output), "--require-classified"]) == 0
    assert json.loads(capsys.readouterr().out)["classified_total"] == 1


def test_public_cli_refuses_output_outside_ignored_corpus_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    outside = repo / "manual-review.json"

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(outside),
        ]
    )

    assert result == 2
    assert not outside.exists()
    assert ".elspeth/tier-model-corpus" in capsys.readouterr().err


def test_public_cli_refuses_symlinked_corpus_directory_outside_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    corpus_dir = repo / ".elspeth/tier-model-corpus"
    corpus_dir.symlink_to(outside, target_is_directory=True)
    output = corpus_dir / "escaped.json"

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not (outside / "escaped.json").exists()
    assert "inside --repo-root" in capsys.readouterr().err


def test_public_cli_refuses_overwrite_without_explicit_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    common = [
        "create",
        str(bundle_path),
        "--repo-root",
        str(repo),
        "--root",
        str(root),
        "--allowlist-dir",
        str(allowlist_dir),
        "--output",
        str(output),
    ]
    assert corpus_main(common) == 0
    original = output.read_bytes()
    capsys.readouterr()

    assert corpus_main(common) == 2
    assert output.read_bytes() == original
    assert "--overwrite" in capsys.readouterr().err
    assert corpus_main([*common, "--overwrite"]) == 0


def test_public_cli_checks_no_overwrite_inside_atomic_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    atomic_update_called = False

    def simulate_concurrent_create(
        path: Path,
        update: object,
        *,
        create_parent: bool,
    ) -> None:
        nonlocal atomic_update_called
        atomic_update_called = True
        assert path == output
        assert create_parent is False
        assert callable(update)
        update("concurrent corpus")

    monkeypatch.setattr(corpus_module, "atomic_update_text", simulate_concurrent_create, raising=False)

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(output),
        ]
    )

    assert atomic_update_called is True
    assert result == 2
    assert "--overwrite" in capsys.readouterr().err


def test_public_cli_reports_atomic_write_contention_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"

    def reject_contended_write(*args: object, **kwargs: object) -> None:
        raise AtomicWriteConflictError("another writer owns the report lock")

    monkeypatch.setattr(corpus_module, "atomic_update_text", reject_contended_write, raising=False)

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert "another writer owns the report lock" in capsys.readouterr().err


@pytest.mark.parametrize("drift_surface", ("head", "source"))
def test_public_cli_rejects_identity_change_during_live_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    drift_surface: str,
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    stable_head = corpus_module.current_git_head(repo)
    stable_source = source_snapshot_sha256(source_root=root, allowlist_dir=allowlist_dir)
    heads = iter((stable_head, "f" * 40) if drift_surface == "head" else (stable_head, stable_head))
    sources = iter((stable_source, "e" * 64) if drift_surface == "source" else (stable_source, stable_source))
    monkeypatch.setattr(corpus_module, "current_git_head", lambda repo_root: next(heads))
    monkeypatch.setattr(corpus_module, "source_snapshot_sha256", lambda **kwargs: next(sources))

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not output.exists()
    assert "changed while deriving live actions" in capsys.readouterr().err


def test_public_cli_is_key_free_before_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, root, allowlist_dir, bundle_path = _real_staged_repo(tmp_path)
    output = repo / ".elspeth/tier-model-corpus/manual-review.json"
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "operator-secret")

    result = corpus_main(
        [
            "create",
            str(bundle_path),
            "--repo-root",
            str(repo),
            "--root",
            str(root),
            "--allowlist-dir",
            str(allowlist_dir),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not output.exists()
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in capsys.readouterr().err
