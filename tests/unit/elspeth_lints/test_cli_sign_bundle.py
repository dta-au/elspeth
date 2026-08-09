"""``sign-bundle`` -- the operator (key-bearing) firing command.

``sign-bundle`` is the *only* place a judge signature is minted from a staged
review bundle. It re-verifies every staged claim against the live tree, fires
actions into a durable private copy, and publishes the coherent directory only
after final re-verification:

* ``drift_repair`` re-runs the real judge through the ``sign-judge-signatures``
  ceremony (re-judging prevents laundering a stale verdict over drifted content);
* ``new_judgment`` runs the real judge inside the keyed step;
* ``rotation`` mechanically re-binds a *non-judge-gated* key (no judge);
* ``stale_delete`` removes an orphaned entry (no judge).

Deterministic actions run first. A BLOCK/exception/interruption preserves the
active allowlist byte-for-byte and prints a resume command; resume reuses only
authoritatively re-verified signatures.

These tests run with the operator HMAC key PRESENT (so diagnose is authoritative,
unlike the keyless ``test_bundle_verify`` suite); the signing key the fixtures
sign with and the env key are the one shared ``_HMAC_KEY`` constant. The real
judge is patched at the lazy-import seam ``elspeth_lints.core.judge.call_judge``
(patching ``core.cli`` is a no-op -- see ``test_justify.py``).

Fixtures are replicated locally (rather than imported from
``test_judge_signature_diagnosis`` / ``test_bundle_verify``) because there is no
``tests/unit/elspeth_lints`` package and no precedent for cross-test imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from elspeth_lints.core.allowlist import JudgeVerdict, compute_judge_metadata_signature
from elspeth_lints.core.cli import main
from elspeth_lints.core.judge import JUDGE_POLICY_HASH, JudgeResponse
from elspeth_lints.core.judge_signature_diagnosis import diagnose_judge_signatures
from elspeth_lints.core.review_bundle import (
    ActionPreview,
    BundleAction,
    ReviewBundle,
    write_bundle,
)
from elspeth_lints.core.source_snapshot import capture_source_snapshot
from elspeth_lints.rules.trust_tier.tier_model.rotate import identity_prefix

_HMAC_KEY = "x" * 32
_RECORDED_AT = "2024-01-01T00:00:00+00:00"
_MODEL = "claude-opus-4-7"
_RATIONALE = "original judge said the boundary was genuine"


@pytest.fixture(autouse=True)
def _signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # sign-bundle is key-bearing: the operator HMAC key is present so diagnose
    # runs authoritative (recomputes signatures). Override tokens are cleared so
    # the override-required test isolates that one cause.
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", _HMAC_KEY)
    monkeypatch.delenv("ELSPETH_JUDGE_OVERRIDE_TOKEN", raising=False)
    monkeypatch.delenv("ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256", raising=False)


# --------------------------------------------------------------------------- #
# source-tree + allowlist fixtures
# --------------------------------------------------------------------------- #


def _src(doc: str, *, active: bool = True) -> str:
    body = '        return payload.get("name", "anonymous")' if active else '        return "anonymous"'
    return f'"""{doc}"""\n\n\nclass Widget:\n    def lookup(self, payload: dict) -> str:\n{body}\n'


def _build_root(tmp_path: Path) -> Path:
    root = tmp_path / "src_root"
    root.mkdir(parents=True)
    return root


def _write_source(root: Path, rel: str, doc: str, *, active: bool = True) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_src(doc, active=active), encoding="utf-8")
    return target


def _build_allowlist_dir(tmp_path: Path, *, name: str = "allowlist") -> Path:
    # ``name`` is load-bearing for the override-rate audit trail: the judge
    # lanes' decision-event + counter-snapshot side effects only fire inside the
    # shipped ``enforce_*`` aggregation layout (override_rate.py guards on
    # ``allowlist_dir.name.startswith("enforce_")``). The default keeps the
    # no-judge / verify-gate tests on a neutral name; the override-rate
    # regression below opts into the real ``enforce_`` layout.
    allowlist_dir = tmp_path / name
    allowlist_dir.mkdir(parents=True)
    (allowlist_dir / "_defaults.yaml").write_text(
        "version: 1\ndefaults:\n  fail_on_stale: false\n  fail_on_expired: false\n",
        encoding="utf-8",
    )
    return allowlist_dir


def _live_finding(root: Path, rel: str) -> Any:
    from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file

    findings = [f for f in scan_file((root / rel).resolve(), root) if f.rule_id == "R1"]
    if len(findings) != 1:
        raise AssertionError(f"expected one R1 finding in {rel}, got {findings!r}")
    return findings[0]


def _canonical_key(finding: Any) -> str:
    key = finding.canonical_key
    if callable(key):
        key = key()
    if not isinstance(key, str):
        raise AssertionError(f"canonical_key must be str, got {type(key).__name__}")
    return key


def _signed_entry_lines(key: str, *, ast_path: str, scope_fingerprint: str) -> list[str]:
    signature = compute_judge_metadata_signature(
        key=key,
        ast_path=ast_path,
        judge_verdict=JudgeVerdict.ACCEPTED,
        judge_recorded_at=datetime.fromisoformat(_RECORDED_AT),
        judge_model=_MODEL,
        judge_rationale=_RATIONALE,
        judge_policy_hash=JUDGE_POLICY_HASH,
        signature_version=2,
        scope_fingerprint=scope_fingerprint,
        judge_transport="openrouter",
        hmac_key=_HMAC_KEY.encode("utf-8"),
    )
    return [
        f"- key: {key}",
        "  owner: test-owner",
        "  reason: |-",
        "    payload is Tier-3 external data from upstream tool-call",
        "  safety: |-",
        "    Suppression gated by cicd-judge; see judge_rationale below.",
        "  expires: '2030-01-01'",
        "  judge_verdict: ACCEPTED",
        f"  judge_recorded_at: '{_RECORDED_AT}'",
        f"  judge_model: {_MODEL}",
        f"  judge_policy_hash: '{JUDGE_POLICY_HASH}'",
        "  judge_rationale: |-",
        f"    {_RATIONALE}",
        "  judge_signature_version: 2",
        f"  scope_fingerprint: '{scope_fingerprint}'",
        "  judge_transport: openrouter",
        f"  ast_path: '{ast_path}'",
        f"  judge_metadata_signature: '{signature}'",
    ]


def _write_signed_v2_entry(
    allowlist_dir: Path,
    yaml_name: str,
    *,
    finding: Any,
    scope_fingerprint: str | None = None,
) -> str:
    key = _canonical_key(finding)
    stored_scope = finding.scope_fingerprint if scope_fingerprint is None else scope_fingerprint
    lines = ["allow_hits:", *_signed_entry_lines(key, ast_path=finding.ast_path, scope_fingerprint=stored_scope)]
    (allowlist_dir / yaml_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


def _pre_judge_entry_lines(key: str) -> list[str]:
    return [
        f"- key: {key}",
        "  owner: test-owner",
        "  reason: |-",
        "    payload is Tier-3 external data from upstream tool-call",
        "  safety: |-",
        "    suppression",
        "  expires: '2030-01-01'",
    ]


def _write_pre_judge_entry(allowlist_dir: Path, yaml_name: str, *, key: str) -> str:
    lines = ["allow_hits:", *_pre_judge_entry_lines(key)]
    (allowlist_dir / yaml_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


def _stale_rotation_key(finding: Any, *, fp: str = "deadbeefdeadbeef") -> str:
    return identity_prefix(_canonical_key(finding)) + f":fp={fp}"


# A well-formed canonical key for a SPARE pre-judge entry that coexists with the
# drifted signed entry in one YAML. It keeps the file's ``allow_hits`` a non-empty
# list after the drift_repair lane pops the drifted row -- mirroring the canonical
# multi-entry allowlist files, so ``_run_justify``'s similarity scan loads cleanly
# (a bare ``allow_hits:`` with no items is rejected by the loader). It is never a
# bundle action key, so verify ignores it.
_SPARE_PRE_JUDGE_KEY = "plugins/spare.py:R1:Widget:lookup:fp=feedface00000000"
_TRAILING_SPARE_PRE_JUDGE_KEY = "plugins/trailing.py:R1:Widget:lookup:fp=feedface11111111"


def _write_signed_entry_with_spare(
    allowlist_dir: Path,
    yaml_name: str,
    *,
    finding: Any,
    scope_fingerprint: str | None = None,
) -> str:
    """Write a YAML with the (drifted) signed entry between two spare entries.

    Keeping the signed entry in the middle makes sequence position observable:
    drift repair must replace or restore it in place rather than appending it.
    """
    key = _canonical_key(finding)
    stored_scope = finding.scope_fingerprint if scope_fingerprint is None else scope_fingerprint
    lines = [
        "allow_hits:",
        *_pre_judge_entry_lines(_SPARE_PRE_JUDGE_KEY),
        *_signed_entry_lines(key, ast_path=finding.ast_path, scope_fingerprint=stored_scope),
        *_pre_judge_entry_lines(_TRAILING_SPARE_PRE_JUDGE_KEY),
    ]
    (allowlist_dir / yaml_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


# --------------------------------------------------------------------------- #
# bundle + argv helpers
# --------------------------------------------------------------------------- #


def _bundle(
    root: Path,
    allowlist_dir: Path,
    actions: tuple[BundleAction, ...],
    *,
    bundle_id: str = "sign-bundle-under-test",
) -> ReviewBundle:
    repo = Path(os.path.commonpath((root.resolve(), allowlist_dir.resolve())))
    if (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    ):
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "sign-bundle@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Sign Bundle Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    binding = capture_source_snapshot(source_root=root, allowlist_dir=allowlist_dir)
    return ReviewBundle(
        bundle_id=bundle_id,
        schema_version=2,
        created_at="2026-06-28T00:00:00+00:00",
        staged_by="agent-x",
        root=str(root),
        allowlist_dir=str(allowlist_dir),
        source_rev=binding.source_rev,
        source_dirty=binding.source_dirty,
        source_snapshot_sha256=binding.source_snapshot_sha256,
        actions=actions,
    )


def _write_bundle_file(tmp_path: Path, bundle: ReviewBundle) -> Path:
    return write_bundle(bundle, staged_dir=tmp_path / "staged")


def _new_judgment_action(finding: Any, rel: str, *, preview: ActionPreview | None = None) -> BundleAction:
    key = _canonical_key(finding)
    return BundleAction(
        lane="new_judgment",
        kind="justify",
        key=key,
        file_path=rel,
        symbol="Widget.lookup",
        rule="R1",
        fingerprint=key.rsplit(":fp=", 1)[1],
        draft_rationale="payload is Tier-3 external data from upstream tool-call",
        preview=preview,
    )


def _argv(
    bundle_path: Path,
    root: Path,
    allowlist_dir: Path,
    *,
    owner: str = "test-operator",
    extra: tuple[str, ...] = (),
) -> list[str]:
    """Build a ``sign-bundle`` argv bound to this test's tmp_path.

    ``--rotation-log`` defaults to the CWD-relative ``.elspeth/rotations.log``,
    which ``create_transaction`` resolves against the *process* CWD -- under
    pytest that is the checkout, so an unqualified invocation binds the
    repository's own tracked rotation manifest: the transaction snapshots its
    bytes into ``rotation-base.bin``, ``assert_rotation_log_unchanged`` gates on
    them, and a rotation that reaches ``finalize_rotation_log`` appends a
    tmp-dir record to a tracked file. Bind every invocation to the test's
    tmp_path instead (``allowlist_dir.parent``: ``_build_allowlist_dir`` always
    returns ``tmp_path / <name>``), which is the same ``tmp_path /
    "rotations.log"`` the rotation tests already pass explicitly. Callers that
    supply their own ``--rotation-log`` in ``extra`` keep it -- resume runs
    authenticate ``rotation_log`` against the transaction manifest
    (``assert_resume_identity``), so both legs must select the same path.
    """
    rotation_log = () if "--rotation-log" in extra else ("--rotation-log", str(allowlist_dir.parent / "rotations.log"))
    return [
        "sign-bundle",
        str(bundle_path),
        "--root",
        str(root),
        "--allowlist-dir",
        str(allowlist_dir),
        "--owner",
        owner,
        *rotation_log,
        *extra,
    ]


@contextmanager
def _patch_judge(verdict_for: Callable[[str], JudgeVerdict]) -> Iterator[list[str]]:
    """Patch the real judge at the lazy-import seam; dispatch verdict by file_path."""
    calls: list[str] = []

    def _fake(request: Any, **kwargs: Any) -> JudgeResponse:
        calls.append(request.file_path)
        verdict = verdict_for(request.file_path)
        return JudgeResponse(
            verdict=verdict,
            model_id=_MODEL,
            judge_rationale=(
                "re-judged: genuine Tier-3 boundary" if verdict is JudgeVerdict.ACCEPTED else "blocked: not a genuine boundary"
            ),
            recorded_at=datetime.now(UTC),
            should_use_decorator=None,
            confidence=0.91,
            prompt_tokens_total=4000,
            prompt_tokens_cached=0,
            policy_hash=JUDGE_POLICY_HASH,
            judge_transport="openrouter",
        )

    with patch("elspeth_lints.core.judge.call_judge", side_effect=_fake):
        yield calls


def _accept_all(_file_path: str) -> JudgeVerdict:
    return JudgeVerdict.ACCEPTED


def _block_all(_file_path: str) -> JudgeVerdict:
    return JudgeVerdict.BLOCKED


def _diagnose(root: Path, allowlist_dir: Path) -> Any:
    return diagnose_judge_signatures(root=root, allowlist_dir=allowlist_dir)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _recovery_path(stderr: str) -> Path:
    match = re.search(r"--resume\s+('([^']+)'|(\S+))", stderr)
    if match is None:
        raise AssertionError(f"no recovery command in stderr:\n{stderr}")
    return Path(match.group(2) or match.group(3))


# =========================================================================== #
# Task 2.1 -- subparser, dispatch, fail-closed key hoist, load + integrity
# =========================================================================== #


def test_argv_never_selects_the_repository_rotation_manifest(tmp_path: Path) -> None:
    """No sign-bundle invocation may bind the checkout's tracked rotations.log.

    Regression for the CWD-relative ``--rotation-log`` default: a leaked
    binding lets a rotation append a tmp-dir record to a tracked file, which
    dirties the tree and trips pre-commit dirty-checks. Asserted at ``_argv``
    because that is the single chokepoint every test invocation passes through.
    """
    from elspeth_lints.core.cli import _build_parser

    repo_manifest = (Path.cwd() / ".elspeth" / "rotations.log").resolve()
    allowlist_dir = _build_allowlist_dir(tmp_path)
    explicit = str(tmp_path / "explicit-rotations.log")
    for extra in ((), ("--yes",), ("--yes", "--resume", str(tmp_path / "tx")), ("--yes", "--rotation-log", explicit)):
        argv = _argv(tmp_path / "bundle.json", tmp_path / "src_root", allowlist_dir, extra=extra)
        assert argv.count("--rotation-log") == 1, f"{extra!r} produced a duplicate/missing --rotation-log: {argv!r}"
        selected = _build_parser().parse_args(argv).rotation_log.resolve()
        assert selected != repo_manifest, f"{extra!r} selected the repository rotation manifest"
        assert tmp_path.resolve() in selected.parents, f"{extra!r} selected {selected} outside tmp_path"


def test_sign_bundle_fails_closed_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """[O1] §5.4: a keyless run aborts before any tree read, even stale_delete-only."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="stale_delete", key=key, source_file="widget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    monkeypatch.delenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", raising=False)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 2
    assert "ELSPETH_JUDGE_METADATA_HMAC_KEY" in capsys.readouterr().err


def test_sign_bundle_loads_hmac_key_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--env-file supplies the operator key with the same loader as sign-judge-signatures."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)  # orphan the entry so stale_delete verifies
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="stale_delete", key=key, source_file="widget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    env_file = tmp_path / "operator.env"
    env_file.write_text(f'ELSPETH_JUDGE_METADATA_HMAC_KEY="{_HMAC_KEY}"\nUNRELATED_KEY="ignored"\n', encoding="utf-8")
    monkeypatch.delenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", raising=False)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--env-file", str(env_file)))) == 0
    assert "UNRELATED_KEY" not in os.environ


def test_sign_bundle_rejects_missing_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A bad --env-file path aborts before the key hoist and before any tree read."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    bundle_path = tmp_path / "unread.json"  # never read: env-file rejection comes first
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--env-file", str(tmp_path / "absent.env")))) == 2
    assert "--env-file" in capsys.readouterr().err


def test_sign_bundle_rejects_malformed_bundle(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    bundle_path = tmp_path / "bad.json"
    bundle_path.write_text(json.dumps({"actions": []}), encoding="utf-8")  # missing schema_version
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 2


def test_sign_bundle_rejects_readonly_judge_tools(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="stale_delete", key=key, source_file="widget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--judge-tools", "readonly"))) == 2
    assert "readonly" in capsys.readouterr().err


# =========================================================================== #
# Task 2.2 -- re-verify gate (abort before any write)
# =========================================================================== #


def test_sign_bundle_aborts_on_tree_drift_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    # Tree reports SCOPE_BINDING_DRIFT (synthetic scope drift)...
    key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding, scope_fingerprint="b" * 64)
    yaml_path = allowlist_dir / "widget.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    # ...but the bundle claims it is only positional AST drift.
    bundle = _bundle(
        root, allowlist_dir, (BundleAction(lane="resign", kind="drift_repair", key=key, diagnosis_status="AST_PATH_BINDING_DRIFT"),)
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 2
    err = capsys.readouterr().err
    assert "SCOPE_BINDING_DRIFT" in err
    assert yaml_path.read_text(encoding="utf-8") == before  # no write


def test_sign_bundle_aborts_before_transaction_when_target_census_is_incomplete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    bundle_path = _write_bundle_file(tmp_path, _bundle(root, allowlist_dir, ()))
    before = _tree_bytes(allowlist_dir)

    with _patch_judge(_accept_all) as judge_calls:
        exit_code = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert exit_code == 2
    assert judge_calls == []
    assert _tree_bytes(allowlist_dir) == before
    assert not (allowlist_dir.parent / ".sign-bundle-transactions").exists()
    assert "target census missing justify action" in capsys.readouterr().err


# =========================================================================== #
# Task 2.3 -- resign lane (drift_repair re-judges; rotation/stale_delete no judge)
# =========================================================================== #


def _drift_repair_ast_path_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    """A signed entry whose live AST position shifted -> AST_PATH_BINDING_DRIFT.

    The signed v2 entry binds the pre-shim ast_path; prepending a real statement
    shifts ``Module.body`` so ast_path drifts while the enclosing scope content
    stays byte-identical (scope_fingerprint stable).
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    src = _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_entry_with_spare(allowlist_dir, "plugins.yaml", finding=finding)
    src.write_text("_SHIM = 1\n\n\n" + _src("widget"), encoding="utf-8")
    return root, allowlist_dir, key


def test_sign_bundle_drift_repair_rejudges(tmp_path: Path) -> None:
    root, allowlist_dir, key = _drift_repair_ast_path_fixture(tmp_path)
    # Sanity: the tree genuinely reports the claimed status.
    assert any(i.status == "AST_PATH_BINDING_DRIFT" for i in _diagnose(root, allowlist_dir).items)
    bundle = _bundle(
        root, allowlist_dir, (BundleAction(lane="resign", kind="drift_repair", key=key, diagnosis_status="AST_PATH_BINDING_DRIFT"),)
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    with _patch_judge(_accept_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert calls == ["plugins/widget.py"]  # the real judge WAS re-run
    post = _diagnose(root, allowlist_dir)
    assert not any(i.status == "AST_PATH_BINDING_DRIFT" for i in post.items)
    assert any(i.status == "OK_AUTHORITATIVE" for i in post.items)
    repaired_key = next(i.key for i in post.items if i.status == "OK_AUTHORITATIVE")
    import yaml

    written = yaml.safe_load((allowlist_dir / "plugins.yaml").read_text(encoding="utf-8"))
    assert [entry["key"] for entry in written["allow_hits"]] == [
        _SPARE_PRE_JUDGE_KEY,
        repaired_key,
        _TRAILING_SPARE_PRE_JUDGE_KEY,
    ]


def test_sign_bundle_drift_repair_block_not_laundered(tmp_path: Path) -> None:
    """§5.5/§7: an honest SCOPE drift that the judge BLOCKs is not laundered.

    The reused ceremony pops the stale row before judging and re-appends it on
    judge failure -- a pop-WITHOUT-restore would silently delete a signed entry.
    Pin the pop->block->restore contract by byte-comparing the YAML survives.
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_entry_with_spare(allowlist_dir, "plugins.yaml", finding=finding, scope_fingerprint="b" * 64)
    yaml_path = allowlist_dir / "plugins.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    assert any(i.status == "SCOPE_BINDING_DRIFT" for i in _diagnose(root, allowlist_dir).items)
    bundle = _bundle(
        root, allowlist_dir, (BundleAction(lane="resign", kind="drift_repair", key=key, diagnosis_status="SCOPE_BINDING_DRIFT"),)
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    with _patch_judge(_block_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc != 0
    assert calls == ["plugins/widget.py"]  # judge ran and BLOCKed
    assert yaml_path.read_text(encoding="utf-8") == before  # restored intact -- NOT deleted, NOT re-signed
    assert "b" * 64 in before  # the original drifted scope binding is still on disk


def test_sign_bundle_drift_repair_block_records_override_rate_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A BLOCKed drift_repair must leave its ``blocked_without_override`` record in
    the override-rate decision-events trail -- the governance SIDE EFFECT, sibling
    of the rotation-manifest pin.

    This is the sole-provenance case: the lane restores the YAML byte-identically
    after a block (the test above), so the live-YAML recompute path inside
    ``compute_override_rate`` sees NO trace of the block. The decision-events JSONL
    is the *only* record, and the gate reads it exclusively for
    ``blocked_without_override_in_window`` (override_rate.py). A refactor that
    dropped the ``_run_justify`` decision-event write would keep the
    block_not_laundered pin GREEN while silently undercounting judge blocks in the
    override-rate gate -- exactly the ``assert-the-write, ignore-the-side-effect``
    class that let the rotation-manifest bug ship. The ``enforce_`` dir name is
    load-bearing: ``append_judge_decision_event`` no-ops outside that layout.
    """
    from elspeth_lints.core.override_rate import judge_decision_events_path

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    key = _write_signed_entry_with_spare(allowlist_dir, "plugins.yaml", finding=finding, scope_fingerprint="b" * 64)
    yaml_path = allowlist_dir / "plugins.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    assert any(i.status == "SCOPE_BINDING_DRIFT" for i in _diagnose(root, allowlist_dir).items)
    bundle = _bundle(
        root, allowlist_dir, (BundleAction(lane="resign", kind="drift_repair", key=key, diagnosis_status="SCOPE_BINDING_DRIFT"),)
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    with _patch_judge(_block_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc != 0
    assert calls == ["plugins/widget.py"]  # judge ran and BLOCKed
    assert yaml_path.read_text(encoding="utf-8") == before  # YAML restored -> live recompute sees no block

    transaction = _recovery_path(capsys.readouterr().err)
    events_path = judge_decision_events_path(next(path for path in transaction.rglob("enforce_tier_model") if path.is_dir()))
    assert events_path.exists(), "drift_repair BLOCKED but preserved no override-rate decision event in the recovery transaction"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(
        event["entry_key"] == key
        and event["effective_verdict"] == JudgeVerdict.BLOCKED.value
        and event["write_disposition"] == "blocked_without_override"
        for event in events
    ), f"no blocked_without_override decision event for {key!r} in {events!r}"


def test_sign_bundle_rotation_no_judge(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    def _raise(_file_path: str) -> JudgeVerdict:
        raise AssertionError("the judge must not run for a rotation action")

    with _patch_judge(_raise):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--rotation-log", str(tmp_path / "rotations.log"))))

    assert rc == 0
    text = (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    assert f"- key: {live_key}" in text
    assert stale_key not in text


def test_sign_bundle_rotation_records_rotation_manifest(tmp_path: Path) -> None:
    """A rotation action MUST append the .elspeth/rotations.log manifest record the
    governance gate (check-rotation-audit) consumes -- the gate derives expected
    rotations from the git diff of the allowlist and fails any old->new key change
    with no covering manifest record. Regression for the hardcoded
    ``rotation_log_path=None`` that rewrote the key but suppressed the manifest.
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    rot_log = tmp_path / "rotations.log"
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    publish_window_start = datetime.now(UTC)
    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--rotation-log", str(rot_log))))
    publish_window_end = datetime.now(UTC)

    assert rc == 0
    assert f"- key: {live_key}" in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    assert rot_log.exists(), "rotation applied but no .elspeth/rotations.log manifest record was written -- check-rotation-audit will fail"
    records = [json.loads(line) for line in rot_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    rotations = [item for rec in records for item in rec.get("rotations", [])]
    assert {"source_file": "gadget.yaml", "old_key": stale_key, "new_key": live_key} in rotations
    rotation_recorded_at = datetime.fromisoformat(records[0]["recorded_at"])
    assert publish_window_start <= rotation_recorded_at <= publish_window_end


def test_sign_bundle_rotation_log_conflict_fails_before_active_publish(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    before = _tree_bytes(allowlist_dir)
    rotation_log = tmp_path / "rotations.log"
    rotation_log.write_text('{"base":true}\n', encoding="utf-8")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    real_rotation = cli_module._execute_rotation_action

    def _rotate_then_conflict(action: Any, *, rotation_plan: Any, args: Any) -> int:
        code = real_rotation(action, rotation_plan=rotation_plan, args=args)
        rotation_log.write_text('{"external":true}\n', encoding="utf-8")
        return code

    with patch.object(cli_module, "_execute_rotation_action", side_effect=_rotate_then_conflict):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log)),
            )
        )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert _recovery_path(capsys.readouterr().err).is_dir()


def test_sign_bundle_aborts_before_transaction_judge_or_write_on_harmless_source_drift(tmp_path: Path) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    source = _write_source(root, "plugins/clean.py", "clean", active=False)
    bundle_path = _write_bundle_file(tmp_path, _bundle(root, allowlist_dir, ()))
    before = _tree_bytes(allowlist_dir)
    source.write_text(source.read_text(encoding="utf-8") + "# harmless comment\n", encoding="utf-8")

    with (
        patch.object(sign_bundle_transaction, "create_transaction") as create_transaction,
        patch("elspeth_lints.core.judge.call_judge") as call_judge,
    ):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 2
    create_transaction.assert_not_called()
    call_judge.assert_not_called()
    assert _tree_bytes(allowlist_dir) == before


def test_sign_bundle_resume_replays_rotation_interrupted_before_audit_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from elspeth_lints.rules.trust_tier.tier_model import rotate

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    before = _tree_bytes(allowlist_dir)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )

    with patch.object(rotate, "_append_rotation_manifest", side_effect=KeyboardInterrupt()):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log)),
            )
        )

    assert rc == 130
    assert _tree_bytes(allowlist_dir) == before
    transaction = _recovery_path(capsys.readouterr().err)

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 0
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    records = [json.loads(line) for line in rotation_log.read_text(encoding="utf-8").splitlines()]
    assert any({"source_file": "gadget.yaml", "old_key": stale_key, "new_key": live_key} in record["rotations"] for record in records)
    assert all(record["allowlist_dir"] == str(allowlist_dir.resolve()) for record in records)


def test_sign_bundle_resume_finalizes_rotation_audit_after_published_interruption(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )

    with patch.object(
        sign_bundle_transaction,
        "finalize_rotation_log",
        side_effect=KeyboardInterrupt(),
    ):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log)),
            )
        )

    assert rc == 130
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    assert not rotation_log.exists()
    transaction = _recovery_path(capsys.readouterr().err)
    # The exchange committed, then the process died before the audit append.
    # A coordinated writer legitimately mutates the newly active tree before
    # resume; recovery must preserve it and still finalize the committed audit.
    import elspeth_lints.core.cli as cli_module

    cli_module._append_entry_to_yaml(
        allowlist_dir / "later.yaml",
        "\n".join(_pre_judge_entry_lines(_SPARE_PRE_JUDGE_KEY)) + "\n",
    )
    rotation_log.write_text('{"kind":"external_append"}\n', encoding="utf-8")

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 0
    records = [json.loads(line) for line in rotation_log.read_text(encoding="utf-8").splitlines()]
    assert {"kind": "external_append"} in records
    assert any(
        {"source_file": "gadget.yaml", "old_key": stale_key, "new_key": live_key} in record.get("rotations", []) for record in records
    )
    rotation_records = [record for record in records if record.get("kind") == "tier_model_rotation"]
    assert all(record["allowlist_dir"] == str(allowlist_dir.resolve()) for record in rotation_records)
    assert _SPARE_PRE_JUDGE_KEY in (allowlist_dir / "later.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("published_before_interrupt", [False, True])
def test_sign_bundle_resume_migrates_legacy_pending_publication_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    published_before_interrupt: bool,
) -> None:
    """Schema-v1 journals without the new field recover in either orientation."""
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    real_publish = sign_bundle_transaction.publish_candidate

    def _interrupt_publish(transaction: Path, manifest: dict[str, Any]) -> None:
        if published_before_interrupt:
            real_publish(transaction, manifest)
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", _interrupt_publish)
    assert (
        main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log)),
            )
        )
        == 130
    )
    transaction = _recovery_path(capsys.readouterr().err)

    # Simulate an authenticated pre-upgrade schema-v1 journal. The historic
    # manifest had publish_started_at but no explicit source-validation field.
    manifest = sign_bundle_transaction.load_manifest(transaction)
    manifest.pop("source_validation_state")
    sign_bundle_transaction.save_manifest(transaction, manifest)
    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", real_publish)

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 0
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    records = [json.loads(line) for line in rotation_log.read_text(encoding="utf-8").splitlines()]
    assert any({"source_file": "gadget.yaml", "old_key": stale_key, "new_key": live_key} in record["rotations"] for record in records)


def test_sign_bundle_resume_does_not_roll_back_completed_legacy_rotation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )

    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--rotation-log", str(rotation_log)))) == 0
    transaction = _recovery_path(capsys.readouterr().err)
    manifest = sign_bundle_transaction.load_manifest(transaction)
    manifest.pop("source_validation_state")
    sign_bundle_transaction.save_manifest(transaction, manifest)
    before_allowlist = _tree_bytes(allowlist_dir)
    before_rotation = rotation_log.read_bytes()

    _write_source(root, "plugins/gadget.py", "gadget", active=False)
    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before_allowlist
    assert rotation_log.read_bytes() == before_rotation
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")


def test_publication_disposition_rejects_preexchange_writer_and_base_mimic(
    tmp_path: Path,
) -> None:
    import elspeth_lints.core.cli as cli_module
    from elspeth_lints.core import sign_bundle_transaction

    active = _build_allowlist_dir(tmp_path)
    tx_path = tmp_path / "tx"
    candidate = tx_path / "candidate" / active.name
    candidate.parent.mkdir(parents=True)
    shutil.copytree(active, candidate)
    (candidate / "_defaults.yaml").write_text(
        (candidate / "_defaults.yaml").read_text(encoding="utf-8") + "# candidate\n",
        encoding="utf-8",
    )
    manifest = {
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "base_snapshot": sign_bundle_transaction.tree_snapshot(active),
        "candidate_snapshot": sign_bundle_transaction.tree_snapshot(candidate),
        "base_directory_identity": sign_bundle_transaction.directory_identity(active),
        "candidate_directory_identity": sign_bundle_transaction.directory_identity(candidate),
        "publish_started_at": datetime.now(UTC).isoformat(),
    }
    # Mimic post-publish bytes without exchanging directory identities: a
    # scratch writer restores base bytes in-place while a coordinated writer
    # advances the still-old active tree.
    for child in candidate.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(active, candidate, dirs_exist_ok=True)
    cli_module._append_entry_to_yaml(
        active / "later.yaml",
        "\n".join(_pre_judge_entry_lines(_SPARE_PRE_JUDGE_KEY)) + "\n",
    )

    with pytest.raises(
        sign_bundle_transaction.SignBundleTransactionError,
        match="cannot reconcile transaction publication",
    ):
        sign_bundle_transaction.publication_disposition(manifest)


def test_publish_rejects_byte_identical_active_directory_replacement(
    tmp_path: Path,
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    active = _build_allowlist_dir(tmp_path)
    tx_path = tmp_path / "tx"
    candidate = tx_path / "candidate" / active.name
    candidate.parent.mkdir(parents=True)
    shutil.copytree(active, candidate)
    (candidate / "_defaults.yaml").write_text(
        (candidate / "_defaults.yaml").read_text(encoding="utf-8") + "# candidate\n",
        encoding="utf-8",
    )
    manifest = {
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "base_snapshot": sign_bundle_transaction.tree_snapshot(active),
        "candidate_snapshot": sign_bundle_transaction.tree_snapshot(candidate),
        "base_directory_identity": sign_bundle_transaction.directory_identity(active),
        "candidate_directory_identity": sign_bundle_transaction.directory_identity(candidate),
    }

    displaced = tmp_path / "displaced-active"
    active.rename(displaced)
    shutil.copytree(displaced, active)
    assert sign_bundle_transaction.tree_snapshot(active) == manifest["base_snapshot"]
    assert sign_bundle_transaction.directory_identity(active) != manifest["base_directory_identity"]

    with pytest.raises(sign_bundle_transaction.SignBundleTransactionError, match="directory identity"):
        sign_bundle_transaction.publish_candidate(tx_path, manifest)


def test_transaction_lock_rejects_symlinked_transaction_root(tmp_path: Path) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    external = tmp_path / "external-transaction-storage"
    external.mkdir()
    sign_bundle_transaction.transaction_root(allowlist_dir).symlink_to(external, target_is_directory=True)

    with (
        pytest.raises(sign_bundle_transaction.SignBundleTransactionError, match="not a directory"),
        sign_bundle_transaction.transaction_lock(allowlist_dir, create=True),
    ):
        pytest.fail("symlinked transaction root must not be entered")

    assert list(external.iterdir()) == []
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(sign_bundle_transaction.SignBundleTransactionError, match="not a directory"):
        sign_bundle_transaction.create_transaction(
            bundle_path=bundle_path,
            verified_bundle_sha256=sign_bundle_transaction.file_sha256(bundle_path),
            bundle_id="symlink-root",
            root=root,
            allowlist_dir=allowlist_dir,
            rotation_log=tmp_path / "rotations.log",
            signing_policy={"operator_override": False},
        )
    assert list(external.iterdir()) == []


def test_create_transaction_fsyncs_each_new_parent_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{}\n", encoding="utf-8")
    fsynced: list[Path] = []
    real_fsync_directory = sign_bundle_transaction._fsync_directory

    def _record_fsync(path: Path) -> None:
        fsynced.append(path.resolve())
        real_fsync_directory(path)

    monkeypatch.setattr(sign_bundle_transaction, "_fsync_directory", _record_fsync)
    _tx_path, manifest = sign_bundle_transaction.create_transaction(
        bundle_path=bundle_path,
        verified_bundle_sha256=sign_bundle_transaction.file_sha256(bundle_path),
        bundle_id="fsync-order",
        root=root,
        allowlist_dir=allowlist_dir,
        rotation_log=tmp_path / "rotations.log",
        signing_policy={"operator_override": False},
    )

    active_parent = allowlist_dir.resolve().parent
    tx_root = sign_bundle_transaction.transaction_root(allowlist_dir).resolve()
    candidate_parent = Path(manifest["candidate_dir"]).resolve().parent
    assert fsynced.index(active_parent) < fsynced.index(tx_root) < fsynced.index(candidate_parent)


def test_sign_bundle_rejects_bundle_replaced_after_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The recovery transaction must bind the exact bundle the operator confirmed."""
    import io

    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    gadget_finding = _live_finding(root, "plugins/gadget.py")
    gadget_stale = _stale_rotation_key(gadget_finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=gadget_stale)

    _write_source(root, "plugins/sprocket.py", "sprocket")
    sprocket_finding = _live_finding(root, "plugins/sprocket.py")
    sprocket_live = _canonical_key(sprocket_finding)
    sprocket_stale = _stale_rotation_key(sprocket_finding, fp="cafebabecafebabe")
    _write_pre_judge_entry(allowlist_dir, "sprocket.yaml", key=sprocket_stale)

    approved = _bundle(
        root,
        allowlist_dir,
        (
            BundleAction(lane="resign", kind="rotation", key=gadget_stale, source_file="gadget.yaml"),
            BundleAction(lane="resign", kind="rotation", key=sprocket_stale, source_file="sprocket.yaml"),
        ),
    )
    replacement = _bundle(
        root,
        allowlist_dir,
        (
            BundleAction(lane="resign", kind="rotation", key=sprocket_stale, source_file="sprocket.yaml"),
            BundleAction(lane="resign", kind="rotation", key=gadget_stale, source_file="gadget.yaml"),
        ),
    )
    bundle_path = _write_bundle_file(tmp_path, approved)
    approved_bytes = bundle_path.read_bytes()
    rotation_log = tmp_path / "rotations.log"

    class _ReplaceAtConfirmation(io.StringIO):
        def readline(self, *args: Any, **kwargs: Any) -> str:
            _write_bundle_file(tmp_path, replacement)
            return super().readline(*args, **kwargs)

    real_run_transaction = sign_bundle_transaction.run_sign_bundle_transaction
    monkeypatch.setattr("sys.stdin", _ReplaceAtConfirmation("yes\n"))
    monkeypatch.setattr(
        sign_bundle_transaction,
        "run_sign_bundle_transaction",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    first_rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--rotation-log", str(rotation_log)),
        )
    )
    first_stderr = capsys.readouterr().err

    if first_rc != 2:
        transaction = _recovery_path(first_stderr)
        manifest = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        replacement_bytes = bundle_path.read_bytes()
        bound_replacement = manifest["bundle_sha256"] == hashlib.sha256(replacement_bytes).hexdigest()
        monkeypatch.setattr(sign_bundle_transaction, "run_sign_bundle_transaction", real_run_transaction)
        resume_rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
            )
        )
        replacement_executed = sprocket_live in (allowlist_dir / "sprocket.yaml").read_text(encoding="utf-8")
        pytest.fail(
            "bundle replacement crossed the confirmation boundary: "
            f"first_rc={first_rc}, manifest_bound_replacement={bound_replacement}, "
            f"resume_rc={resume_rc}, replacement_executed={replacement_executed}"
        )

    assert "bundle bytes changed after verification" in first_stderr
    assert bundle_path.read_bytes() != approved_bytes
    assert gadget_stale in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")
    assert sprocket_stale in (allowlist_dir / "sprocket.yaml").read_text(encoding="utf-8")


def test_manifest_rejects_authenticated_non_integer_directory_identity(
    tmp_path: Path,
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{}\n", encoding="utf-8")
    tx_path, manifest = sign_bundle_transaction.create_transaction(
        bundle_path=bundle_path,
        verified_bundle_sha256=sign_bundle_transaction.file_sha256(bundle_path),
        bundle_id="identity-type",
        root=root,
        allowlist_dir=allowlist_dir,
        rotation_log=tmp_path / "rotations.log",
        signing_policy={"operator_override": False},
    )
    manifest["base_directory_identity"]["st_ino"] = True
    sign_bundle_transaction.save_manifest(tx_path, manifest)

    with pytest.raises(
        sign_bundle_transaction.SignBundleTransactionError,
        match="strict directory identity",
    ):
        sign_bundle_transaction.load_manifest(tx_path)


def test_sign_bundle_rejects_incomplete_rotation_inventory_before_execute(tmp_path: Path) -> None:
    """The complete census rejects omitted drift and rotation work before writes."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)

    # (1) judge-gated, fp-shifted NON-action.
    widget = _write_source(root, "plugins/widget.py", "widget")
    widget_finding = _live_finding(root, "plugins/widget.py")
    _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=widget_finding)
    widget.write_text("_SHIM = 1\n\n\n" + _src("widget"), encoding="utf-8")  # fp drift -> would crash unfiltered scan

    # (2) staged non-judge-gated rotation.
    _write_source(root, "plugins/gadget.py", "gadget")
    gadget_finding = _live_finding(root, "plugins/gadget.py")
    gadget_stale = _stale_rotation_key(gadget_finding, fp="deadbeefdeadbeef")
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=gadget_stale)
    gadget_before = (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")

    # (3) surveyed-but-unstaged non-judge-gated rotation.
    _write_source(root, "plugins/sprocket.py", "sprocket")
    sprocket_finding = _live_finding(root, "plugins/sprocket.py")
    sprocket_stale = _stale_rotation_key(sprocket_finding, fp="cafebabecafebabe")
    _write_pre_judge_entry(allowlist_dir, "sprocket.yaml", key=sprocket_stale)
    sprocket_before = (allowlist_dir / "sprocket.yaml").read_text(encoding="utf-8")

    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="rotation", key=gadget_stale, source_file="gadget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--rotation-log", str(tmp_path / "rotations.log"))))

    assert rc == 2
    assert (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8") == gadget_before
    assert (allowlist_dir / "sprocket.yaml").read_text(encoding="utf-8") == sprocket_before


def test_sign_bundle_stale_delete_removes_entry(tmp_path: Path) -> None:
    """Multi-file allowlist: the action routes to its OWNING source_file."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    # Orphan to delete: a signed entry whose finding has since vanished.
    _write_source(root, "plugins/widget.py", "widget")
    widget_finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=widget_finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)  # finding gone -> NO_MATCHING_FINDING
    # Sibling in a DIFFERENT file that must remain untouched.
    sibling_before = _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key="plugins/gadget.py:R1:Widget:lookup:fp=feedface00000000")
    gadget_before = (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")

    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="stale_delete", key=orphan_key, source_file="widget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert orphan_key not in (allowlist_dir / "widget.yaml").read_text(encoding="utf-8")
    assert (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8") == gadget_before  # sibling intact
    assert sibling_before in gadget_before


def test_sign_bundle_resume_rejects_unrelated_same_yaml_stale_delete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_entry_with_spare(
        allowlist_dir,
        "widget.yaml",
        finding=finding,
    )
    _write_source(root, "plugins/widget.py", "widget", active=False)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                BundleAction(
                    lane="resign",
                    kind="stale_delete",
                    key=orphan_key,
                    source_file="widget.yaml",
                ),
            ),
        ),
    )
    real_delete = cli_module._execute_stale_delete_action

    def _delete_target_and_sibling(
        action: Any,
        *,
        source_file: str,
        args: Any,
    ) -> int:
        assert real_delete(action, source_file=source_file, args=args) == 0
        cli_module._pop_allow_hits_entry(
            args.allowlist_dir / source_file,
            _SPARE_PRE_JUDGE_KEY,
        )
        raise KeyboardInterrupt

    with patch.object(
        cli_module,
        "_execute_stale_delete_action",
        side_effect=_delete_target_and_sibling,
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--resume", str(transaction)),
        )
    )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before


@pytest.mark.parametrize(
    "duplicate_header",
    (
        "allow_hits: # duplicate",
        "allow_hits :",
        '"allow_hits":',
    ),
)
def test_sign_bundle_resume_rejects_duplicate_allow_hits_block(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duplicate_header: str,
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_entry_with_spare(
        allowlist_dir,
        "widget.yaml",
        finding=finding,
    )
    _write_source(root, "plugins/widget.py", "widget", active=False)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                BundleAction(
                    lane="resign",
                    kind="stale_delete",
                    key=orphan_key,
                    source_file="widget.yaml",
                ),
            ),
        ),
    )
    real_delete = cli_module._execute_stale_delete_action

    def _delete_then_duplicate_block(
        action: Any,
        *,
        source_file: str,
        args: Any,
    ) -> int:
        assert real_delete(action, source_file=source_file, args=args) == 0
        target = args.allowlist_dir / source_file
        current = target.read_text(encoding="utf-8")
        duplicate = current.replace("allow_hits:", duplicate_header, 1)
        target.write_text(current + "\n" + duplicate, encoding="utf-8")
        raise KeyboardInterrupt

    with patch.object(
        cli_module,
        "_execute_stale_delete_action",
        side_effect=_delete_then_duplicate_block,
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--resume", str(transaction)),
        )
    )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before


# =========================================================================== #
# Task 2.4 -- new-judgment lane (real judge + sign) + BLOCK + override
# =========================================================================== #


def test_sign_bundle_new_judgment_runs_real_judge(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle = _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    with _patch_judge(_accept_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert calls == ["plugins/gadget.py"]
    post = _diagnose(root, allowlist_dir)
    assert any(i.status == "OK_AUTHORITATIVE" and i.key == _canonical_key(finding) for i in post.items)


def test_sign_bundle_refreshes_override_snapshot_only_after_active_publish(tmp_path: Path) -> None:
    from elspeth_lints.core.override_rate import default_counter_snapshot_path

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )

    with _patch_judge(_accept_all):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert default_counter_snapshot_path(allowlist_dir.parent).is_file()
    tx_root = allowlist_dir.parent / ".sign-bundle-transactions"
    assert not any(path.name == ".judge-metrics" for path in tx_root.rglob(".judge-metrics"))


def test_sign_bundle_block_contradicting_preview_not_signed(tmp_path: Path) -> None:
    """§7: an ACCEPTED preview does not survive a BLOCK from the authoritative judge."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    preview = ActionPreview(verdict="ACCEPTED", rationale="agent preview said genuine", model="preview-model", transport="claude_agent_sdk")
    bundle = _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py", preview=preview),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    with _patch_judge(_block_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc != 0
    assert calls == ["plugins/gadget.py"]
    assert not (allowlist_dir / "plugins.yaml").exists()  # nothing signed
    post = _diagnose(root, allowlist_dir)
    assert all(i.key != _canonical_key(finding) for i in post.items)


def test_sign_bundle_override_token_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle = _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    # --operator-override but no override token in the env (cleared by the fixture).
    with _patch_judge(_accept_all):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--operator-override")))

    assert rc == 2
    assert "ELSPETH_JUDGE_OVERRIDE_TOKEN" in capsys.readouterr().err
    assert not (allowlist_dir / "plugins.yaml").exists()


def test_sign_bundle_partial_block_preserves_active_allowlist_and_reports_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted action followed by BLOCK is recoverable without partial publish."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle = _bundle(
        root,
        allowlist_dir,
        (
            _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
            _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
        ),
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    def _verdict(file_path: str) -> JudgeVerdict:
        return JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED

    with _patch_judge(_verdict):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc != 0
    assert _tree_bytes(allowlist_dir) == before
    err = capsys.readouterr().err
    transaction = _recovery_path(err)
    assert transaction.is_dir()
    assert "preserved" in err.lower()


def test_sign_bundle_resume_reuses_accepted_judgment_and_publishes_coherently(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    alpha = _live_finding(root, "alpha/mod.py")
    beta = _live_finding(root, "beta/mod.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(alpha, "alpha/mod.py"),
                _new_judgment_action(beta, "beta/mod.py"),
            ),
        ),
    )

    with _patch_judge(lambda file_path: JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)
    assert _tree_bytes(allowlist_dir) == before

    with _patch_judge(_accept_all) as resumed_calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 0
    assert resumed_calls == ["beta/mod.py"]
    post = _diagnose(root, allowlist_dir)
    assert any(item.key == _canonical_key(alpha) and item.status == "OK_AUTHORITATIVE" for item in post.items)
    assert any(item.key == _canonical_key(beta) and item.status == "OK_AUTHORITATIVE" for item in post.items)


def test_sign_bundle_resume_finishes_interruption_after_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A kill after the exchange is recovered from bytes, not a stale state flag."""
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    publish_candidate = sign_bundle_transaction.publish_candidate

    def _publish_then_interrupt(transaction: Path, manifest: dict[str, object]) -> None:
        publish_candidate(transaction, manifest)
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", _publish_then_interrupt)
    with _patch_judge(_accept_all):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 130
    assert any(item.key == _canonical_key(finding) and item.status == "OK_AUTHORITATIVE" for item in _diagnose(root, allowlist_dir).items)
    transaction = _recovery_path(capsys.readouterr().err)

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", publish_candidate)
    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("published recovery must not repeat accepted judge work"))):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--resume", str(transaction)),
            )
        )

    assert rc == 0
    assert any(item.key == _canonical_key(finding) and item.status == "OK_AUTHORITATIVE" for item in _diagnose(root, allowlist_dir).items)


def test_sign_bundle_resume_rejects_changed_signing_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )
    with _patch_judge(lambda file_path: JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED):
        assert main(_argv(bundle_path, root, allowlist_dir, owner="operator-a", extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("policy mismatch must not call judge"))):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                owner="operator-b",
                extra=("--yes", "--resume", str(transaction)),
            )
        )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert "policy" in capsys.readouterr().err.lower()


def test_sign_bundle_stale_delete_runs_before_judge_work(tmp_path: Path) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "old/mod.py", "old")
    orphan = _write_signed_v2_entry(allowlist_dir, "old.yaml", finding=_live_finding(root, "old/mod.py"))
    _write_source(root, "old/mod.py", "old", active=False)
    _write_source(root, "new/mod.py", "new")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "new/mod.py"), "new/mod.py"),
                BundleAction(lane="resign", kind="stale_delete", key=orphan, source_file="old.yaml"),
            ),
        ),
    )
    order: list[str] = []
    real_delete = cli_module._execute_stale_delete_action

    def _record_delete(action: Any, *, source_file: str, args: Any) -> int:
        order.append("stale_delete")
        return real_delete(action, source_file=source_file, args=args)

    with (
        patch.object(cli_module, "_execute_stale_delete_action", side_effect=_record_delete),
        _patch_judge(lambda _file_path: order.append("judge") or JudgeVerdict.ACCEPTED),
    ):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert order == ["stale_delete", "judge"]


def test_sign_bundle_does_not_publish_until_every_action_succeeds(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )

    def _accept_while_active_is_unchanged(_file_path: str) -> JudgeVerdict:
        assert _tree_bytes(allowlist_dir) == before
        return JudgeVerdict.ACCEPTED

    with _patch_judge(_accept_while_active_is_unchanged):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert (allowlist_dir / "alpha.yaml").is_file()
    assert (allowlist_dir / "beta.yaml").is_file()


def test_sign_bundle_rejects_unrelated_candidate_mutation(tmp_path: Path) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    real_execute = cli_module._execute_new_judgment_action

    def _execute_then_tamper(action: Any, *, args: Any) -> int:
        code = real_execute(action, args=args)
        (args.allowlist_dir / "unrelated.yaml").write_text(
            "allow_hits: []\n",
            encoding="utf-8",
        )
        return code

    with (
        _patch_judge(_accept_all),
        patch.object(
            cli_module,
            "_execute_new_judgment_action",
            side_effect=_execute_then_tamper,
        ),
    ):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before


@pytest.mark.parametrize(
    "tamper",
    ("unrelated_record", "impossible_verdict_pair", "naive_timestamp"),
)
def test_sign_bundle_rejects_judge_decision_event_rewrite(
    tmp_path: Path,
    tamper: str,
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    real_execute = cli_module._execute_new_judgment_action

    def _execute_then_rewrite_events(action: Any, *, args: Any) -> int:
        code = real_execute(action, args=args)
        event_path = args.allowlist_dir / ".judge-metrics" / "judge-decision-events.jsonl"
        record = json.loads(event_path.read_text(encoding="utf-8"))
        if tamper == "impossible_verdict_pair":
            record["effective_verdict"] = "ACCEPTED"
            record["model_verdict"] = "BLOCKED"
        elif tamper == "naive_timestamp":
            record["recorded_at"] = "2026-01-01T00:00:00"
        else:
            record = {
                "schema_version": 1,
                "source_file": "unrelated.py",
                "entry_key": "unrelated.py:R1:X:y:fp=deadbeefdeadbeef",
                "rule_id": "R1",
                "effective_verdict": "ACCEPTED",
                "model_verdict": "ACCEPTED",
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "write_disposition": "written",
            }
        event_path.write_text(
            json.dumps(record, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return code

    with (
        _patch_judge(_accept_all),
        patch.object(
            cli_module,
            "_execute_new_judgment_action",
            side_effect=_execute_then_rewrite_events,
        ),
    ):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before


def test_sign_bundle_resume_rejects_written_event_for_incomplete_action(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    real_execute = cli_module._execute_new_judgment_action

    def _execute_then_remove_written_entry(action: Any, *, args: Any) -> int:
        assert real_execute(action, args=args) == 0
        cli_module._pop_allow_hits_entry(
            args.allowlist_dir / "plugins.yaml",
            action.key,
        )
        raise KeyboardInterrupt

    with (
        _patch_judge(_accept_all),
        patch.object(
            cli_module,
            "_execute_new_judgment_action",
            side_effect=_execute_then_remove_written_entry,
        ),
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)

    with _patch_judge(_accept_all):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--resume", str(transaction)),
            )
        )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before


def test_sign_bundle_resume_reconstructs_missing_event_without_rejudging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )

    with (
        _patch_judge(_accept_all),
        patch.object(
            cli_module,
            "_append_judge_decision_event_after_judge",
            side_effect=KeyboardInterrupt(),
        ),
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("authoritatively signed decision must not be re-judged"))):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--resume", str(transaction)),
            )
        )

    assert rc == 0
    event_path = allowlist_dir / ".judge-metrics" / "judge-decision-events.jsonl"
    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["entry_key"] == _canonical_key(finding)
    assert records[0]["write_disposition"] == "written"


def test_sign_bundle_resume_rejects_success_event_that_contradicts_signed_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )

    with (
        _patch_judge(_accept_all),
        patch.object(cli_module, "_emit_justify_output", side_effect=KeyboardInterrupt()),
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    event_path = next(transaction.rglob("judge-decision-events.jsonl"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["effective_verdict"] = "OVERRIDDEN_BY_OPERATOR"
    event["model_verdict"] = "BLOCKED"
    event_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("completed signed decision must not be re-judged"))):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--resume", str(transaction)),
            )
        )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert "contradicts its authoritative signed entry" in capsys.readouterr().err


@pytest.mark.parametrize("tamper", ("unrelated_record", "naive_timestamp"))
def test_sign_bundle_rejects_unrelated_staged_rotation_record(
    tmp_path: Path,
    tamper: str,
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    before = _tree_bytes(allowlist_dir)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                BundleAction(
                    lane="resign",
                    kind="rotation",
                    key=stale_key,
                    source_file="gadget.yaml",
                ),
            ),
        ),
    )
    real_rotation = cli_module._execute_rotation_action

    def _rotate_then_append_unrelated(
        action: Any,
        *,
        rotation_plan: Any,
        args: Any,
    ) -> int:
        code = real_rotation(action, rotation_plan=rotation_plan, args=args)
        records = [json.loads(line) for line in args.rotation_log.read_text(encoding="utf-8").splitlines()]
        if tamper == "naive_timestamp":
            records[-1]["recorded_at"] = "2026-01-01T00:00:00"
        else:
            records.append(
                {
                    "schema_version": 1,
                    "kind": "tier_model_rotation",
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                    "allowlist_dir": str(args.allowlist_dir),
                    "rotations": [
                        {
                            "source_file": "other.yaml",
                            "old_key": "other.py:R1:X:y:fp=deadbeefdeadbeef",
                            "new_key": "other.py:R1:X:y:fp=feedfacefeedface",
                        }
                    ],
                    "stale_entries_removed": [],
                    "applied": {
                        "other.yaml": {
                            "rotations_applied": 1,
                            "stale_entries_removed": 0,
                        }
                    },
                }
            )
        args.rotation_log.write_text(
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )
        return code

    with patch.object(
        cli_module,
        "_execute_rotation_action",
        side_effect=_rotate_then_append_unrelated,
    ):
        rc = main(
            _argv(
                bundle_path,
                root,
                allowlist_dir,
                extra=("--yes", "--rotation-log", str(rotation_log)),
            )
        )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert not rotation_log.exists()


@pytest.mark.parametrize(
    ("raised", "expected_rc"),
    [(RuntimeError("judge exploded"), 2), (KeyboardInterrupt(), 130)],
)
def test_sign_bundle_unexpected_exception_or_interrupt_preserves_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raised: BaseException,
    expected_rc: int,
) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )
    calls = 0

    def _accept_then_raise(_file_path: str) -> JudgeVerdict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return JudgeVerdict.ACCEPTED
        raise raised

    with _patch_judge(_accept_then_raise):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == expected_rc
    assert _tree_bytes(allowlist_dir) == before
    assert _recovery_path(capsys.readouterr().err).is_dir()


def test_sign_bundle_resume_rejects_stale_source_before_publish(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    beta_path = _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )
    with _patch_judge(lambda file_path: JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)
    beta_path.write_text("_SHIM = 1\n\n" + beta_path.read_text(encoding="utf-8"), encoding="utf-8")

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("stale resume must not call judge"))):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert "staged claims no longer match" in capsys.readouterr().err


def test_sign_bundle_source_observation_oserror_is_normal_verify_failure_without_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/widget.py"),)),
    )
    before = _tree_bytes(allowlist_dir)

    with (
        patch("elspeth_lints.core.source_snapshot.subprocess.run", side_effect=OSError("git unavailable")),
        patch("elspeth_lints.core.sign_bundle_transaction.create_transaction") as create_transaction,
    ):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    stderr = capsys.readouterr().err
    assert rc == 2
    assert "sign-bundle: verify error:" in stderr
    assert "Traceback" not in stderr
    create_transaction.assert_not_called()
    assert _tree_bytes(allowlist_dir) == before


def test_sign_bundle_rolls_back_if_source_changes_at_directory_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    source_path = _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="stale_delete", key=orphan_key, source_file="widget.yaml"),),
        ),
    )
    real_exchange = sign_bundle_transaction._rename_exchange

    def _change_source_at_exchange(source: Path, destination: Path) -> None:
        if source.resolve() == allowlist_dir.resolve():
            source_path.write_text(_src("widget"), encoding="utf-8")
        real_exchange(source, destination)

    monkeypatch.setattr(sign_bundle_transaction, "_rename_exchange", _change_source_at_exchange)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert orphan_key in (allowlist_dir / "widget.yaml").read_text(encoding="utf-8")
    assert "source tree or bundle bindings changed during coherent publish" in capsys.readouterr().err


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_sign_bundle_resume_rolls_back_pending_publish_if_source_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    legacy_manifest: bool,
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    source_path = _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="stale_delete", key=orphan_key, source_file="widget.yaml"),),
        ),
    )
    real_publish = sign_bundle_transaction.publish_candidate

    def _publish_then_interrupt(transaction: Path, manifest: dict[str, Any]) -> None:
        real_publish(transaction, manifest)
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", _publish_then_interrupt)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    if legacy_manifest:
        manifest = sign_bundle_transaction.load_manifest(transaction)
        manifest.pop("source_validation_state")
        sign_bundle_transaction.save_manifest(transaction, manifest)
    source_path.write_text(_src("widget"), encoding="utf-8")
    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", real_publish)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert orphan_key in (allowlist_dir / "widget.yaml").read_text(encoding="utf-8")
    assert "staged claims no longer match" in capsys.readouterr().err


def test_sign_bundle_resume_rolls_back_pending_publish_if_source_root_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="stale_delete", key=orphan_key, source_file="widget.yaml"),),
        ),
    )
    real_publish = sign_bundle_transaction.publish_candidate

    def _publish_then_interrupt(transaction: Path, manifest: dict[str, Any]) -> None:
        real_publish(transaction, manifest)
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", _publish_then_interrupt)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    shutil.rmtree(root)
    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", real_publish)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert orphan_key in (allowlist_dir / "widget.yaml").read_text(encoding="utf-8")
    assert "verify error" in capsys.readouterr().err


def test_sign_bundle_resume_rejects_tampered_transaction_signature(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    before = _tree_bytes(allowlist_dir)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )
    with _patch_judge(lambda file_path: JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)
    signed_yaml = next(transaction.rglob("alpha.yaml"))
    signed_yaml.write_text(
        signed_yaml.read_text(encoding="utf-8").replace("judge_metadata_signature: '", "judge_metadata_signature: 'tampered"),
        encoding="utf-8",
    )

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("tampered resume must not call judge"))):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert "signature" in capsys.readouterr().err.lower()


def test_sign_bundle_resume_restores_drift_entry_after_interrupt_between_pop_and_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import elspeth_lints.core.cli as cli_module

    root, allowlist_dir, key = _drift_repair_ast_path_fixture(tmp_path)
    before = _tree_bytes(allowlist_dir)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="drift_repair", key=key, diagnosis_status="AST_PATH_BINDING_DRIFT"),),
        ),
    )

    with patch.object(cli_module, "_run_justify", side_effect=KeyboardInterrupt()):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 130
    assert _tree_bytes(allowlist_dir) == before
    transaction = _recovery_path(capsys.readouterr().err)

    with _patch_judge(_accept_all) as resumed_calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 0
    assert resumed_calls == ["plugins/widget.py"]
    assert any(item.status == "OK_AUTHORITATIVE" for item in _diagnose(root, allowlist_dir).items)


# =========================================================================== #
# Task 2.5 -- summary / confirm / dry-run / dup-key / baseline-regen
# =========================================================================== #


def test_sign_bundle_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle = _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    def _raise(_file_path: str) -> JudgeVerdict:
        raise AssertionError("dry-run must not call the judge")

    with _patch_judge(_raise):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--dry-run",)))

    assert rc == 0
    assert not (allowlist_dir / "plugins.yaml").exists()
    assert not (allowlist_dir.parent / ".sign-bundle-transactions").exists()
    out = capsys.readouterr().out
    assert "new_judgment" in out


def test_sign_bundle_resume_dry_run_never_rolls_back_published_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    live_key = _canonical_key(finding)
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    real_publish = sign_bundle_transaction.publish_candidate

    def _interrupt_after_publish(transaction: Path, manifest: dict[str, Any]) -> None:
        real_publish(transaction, manifest)
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", _interrupt_after_publish)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--rotation-log", str(rotation_log)))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    monkeypatch.setattr(sign_bundle_transaction, "publish_candidate", real_publish)
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")

    _write_source(root, "plugins/gadget.py", "gadget", active=False)
    before = _tree_bytes(allowlist_dir)
    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--dry-run", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert live_key in (allowlist_dir / "gadget.yaml").read_text(encoding="utf-8")


def test_sign_bundle_dry_run_reports_planned_override_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    alpha_finding = _live_finding(root, "alpha/mod.py")
    beta_finding = _live_finding(root, "beta/mod.py")
    bundle = _bundle(
        root,
        allowlist_dir,
        (
            _new_judgment_action(alpha_finding, "alpha/mod.py"),
            _new_judgment_action(beta_finding, "beta/mod.py"),
        ),
    )
    bundle_path = _write_bundle_file(tmp_path, bundle)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--dry-run", "--operator-override")))

    assert rc == 0
    out = capsys.readouterr().out
    # K = 2 planned override actions, surfaced as the load-bearing integer.
    assert "planned operator-override actions: 2" in out
    assert "approx" in out.lower()


def test_sign_bundle_requires_confirmation_without_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle = _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))

    def _raise(_file_path: str) -> JudgeVerdict:
        raise AssertionError("a declined confirmation must not call the judge")

    with _patch_judge(_raise):
        rc = main(_argv(bundle_path, root, allowlist_dir))  # no --yes

    assert rc == 0
    assert not (allowlist_dir / "plugins.yaml").exists()  # nothing written


def _dup_key_signed_block(key: str) -> list[str]:
    return _signed_entry_lines(key, ast_path="body[1]/body[0]/body[0]/value", scope_fingerprint="a" * 64)


def test_sign_bundle_dup_key_bundle_aborts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A duplicate-key bundle fails closed before either action can mutate it.

    The same key K appears twice -- once judge-gated (filtered out of the
    non-judge-gated rotation survey) and once non-judge-gated (the staged
    rotation). The full worklist also sees the signed copy as an orphan, so a
    rotation-only bundle is rejected before transaction creation.
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    text = "\n".join(["allow_hits:", *_dup_key_signed_block(stale_key), *_pre_judge_entry_lines(stale_key)]) + "\n"
    yaml_path = allowlist_dir / "gadget.yaml"
    yaml_path.write_text(text, encoding="utf-8")

    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 2
    assert yaml_path.read_text(encoding="utf-8").count(f"- key: {stale_key}") == 2  # both copies preserved
    assert "missing stale_delete action" in capsys.readouterr().err


def test_sign_bundle_noncanonical_allowlist_skips_baseline_regen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hermeticity: a tmp_path run never shells regen_fingerprint_baseline.py."""
    import subprocess

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    widget_finding = _live_finding(root, "plugins/widget.py")
    orphan_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=widget_finding)
    _write_source(root, "plugins/widget.py", "widget", active=False)  # orphan
    bundle = _bundle(root, allowlist_dir, (BundleAction(lane="resign", kind="stale_delete", key=orphan_key, source_file="widget.yaml"),))
    bundle_path = _write_bundle_file(tmp_path, bundle)

    calls: list[Any] = []
    real_run = subprocess.run

    def _spy_run(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr("subprocess.run", _spy_run)

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",)))

    assert rc == 0
    assert not any("regen_fingerprint_baseline.py" in str(call) for call in calls)
    assert "canonical-allowlist-only" in capsys.readouterr().out


def test_sign_bundle_resume_rejects_candidate_and_manifest_co_tamper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keyless attacker cannot bless modified scratch bytes by editing hashes."""
    from elspeth_lints.core.sign_bundle_transaction import tree_snapshot

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "alpha/mod.py", "alpha")
    _write_source(root, "beta/mod.py", "beta")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (
                _new_judgment_action(_live_finding(root, "alpha/mod.py"), "alpha/mod.py"),
                _new_judgment_action(_live_finding(root, "beta/mod.py"), "beta/mod.py"),
            ),
        ),
    )
    with _patch_judge(lambda file_path: JudgeVerdict.ACCEPTED if file_path.startswith("alpha/") else JudgeVerdict.BLOCKED):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)

    manifest_path = transaction / "transaction.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = Path(manifest["candidate_dir"])
    signed_yaml = candidate / "alpha.yaml"
    signed_yaml.write_text(signed_yaml.read_text(encoding="utf-8") + "# keyless tamper\n", encoding="utf-8")
    manifest["candidate_snapshot"] = tree_snapshot(candidate)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("tampered journal must not call judge"))):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert "authentication failed" in capsys.readouterr().err


def test_sign_bundle_resume_rejects_checkpoint_staged_and_manifest_co_tamper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Checkpoint/staged before-images cannot be replaced by a keyless attacker."""
    from elspeth_lints.core import sign_bundle_transaction
    from elspeth_lints.rules.trust_tier.tier_model import rotate

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    with patch.object(rotate, "_append_rotation_manifest", side_effect=KeyboardInterrupt()):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)

    manifest_path = transaction / "transaction.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged = b'{"kind":"tier_model_rotation","rotations":[]}\n'
    checkpoint_staged = transaction / "checkpoint" / "rotation-staged.log"
    checkpoint_staged.write_bytes(checkpoint_staged.read_bytes() + forged)
    staged = transaction / "rotation-staged.log"
    staged.write_bytes(staged.read_bytes() + forged)
    manifest["checkpoint_snapshot"] = sign_bundle_transaction.tree_snapshot(transaction / "checkpoint")
    manifest["rotation_staged_sha256"] = sign_bundle_transaction.file_sha256(staged)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert "authentication failed" in capsys.readouterr().err


def test_publish_waits_for_active_writer_then_rechecks_without_stranding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer paused inside the stable lock lands before publish rechecks."""
    from elspeth_lints.core import cli as cli_module
    from elspeth_lints.core import sign_bundle_transaction

    active = _build_allowlist_dir(tmp_path)
    target = active / "plugins.yaml"
    target.write_text("allow_hits: []\n", encoding="utf-8")
    tx_path = tmp_path / "tx"
    candidate = tx_path / "candidate" / active.name
    candidate.parent.mkdir(parents=True)
    shutil.copytree(active, candidate)
    (candidate / "plugins.yaml").write_text("allow_hits: []\n# candidate\n", encoding="utf-8")
    manifest = {
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "base_snapshot": sign_bundle_transaction.tree_snapshot(active),
        "candidate_snapshot": sign_bundle_transaction.tree_snapshot(candidate),
    }

    writer_paused = threading.Event()
    release_writer = threading.Event()
    real_atomic_update = cli_module.atomic_update_text

    def _paused_update(*args: Any, **kwargs: Any) -> None:
        writer_paused.set()
        assert release_writer.wait(timeout=5)
        real_atomic_update(*args, **kwargs)

    monkeypatch.setattr(cli_module, "atomic_update_text", _paused_update)
    writer = threading.Thread(
        target=cli_module._append_entry_to_yaml,
        args=(target, "- key: plugins/x.py:R1:X:fp=1\n  owner: writer\n"),
        daemon=True,
    )
    writer.start()
    assert writer_paused.wait(timeout=5)

    publish_error: list[BaseException] = []

    def _publish() -> None:
        try:
            sign_bundle_transaction.publish_candidate(tx_path, manifest)
        except BaseException as exc:
            publish_error.append(exc)

    publisher = threading.Thread(target=_publish, daemon=True)
    publisher.start()
    assert publisher.is_alive()
    release_writer.set()
    writer.join(timeout=5)
    publisher.join(timeout=5)

    assert not writer.is_alive()
    assert not publisher.is_alive()
    assert len(publish_error) == 1
    assert "publish precondition failed" in str(publish_error[0])
    assert "plugins/x.py" in target.read_text(encoding="utf-8")
    assert "# candidate" in (candidate / "plugins.yaml").read_text(encoding="utf-8")


def test_sign_bundle_resume_rejects_duplicate_manifest_json_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    with _patch_judge(_block_all):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 1
    transaction = _recovery_path(capsys.readouterr().err)
    manifest_path = transaction / "transaction.json"
    text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        text.replace('"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,', 1),
        encoding="utf-8",
    )

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert "duplicate JSON object key" in capsys.readouterr().err


def test_sign_bundle_resume_rejects_duplicate_judge_event_json_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path, name="enforce_tier_model")
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    with (
        _patch_judge(_accept_all),
        patch.object(cli_module, "_emit_justify_output", side_effect=KeyboardInterrupt()),
    ):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    event_path = next(transaction.rglob("judge-decision-events.jsonl"))
    text = event_path.read_text(encoding="utf-8")
    event_path.write_text(
        text.replace('"entry_key":', f'"entry_key": "{_canonical_key(finding)}", "entry_key":', 1),
        encoding="utf-8",
    )

    rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 2
    assert "valid JSONL" in capsys.readouterr().err


def test_sign_bundle_resume_rejects_duplicate_rotation_event_json_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import elspeth_lints.core.cli as cli_module

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    real_execute = cli_module._execute_rotation_action
    rotation_log = tmp_path / "rotations.log"

    def _execute_then_interrupt(action: Any, *, rotation_plan: Any, args: Any) -> int:
        assert real_execute(action, rotation_plan=rotation_plan, args=args) == 0
        raise KeyboardInterrupt

    with patch.object(cli_module, "_execute_rotation_action", side_effect=_execute_then_interrupt):
        assert (
            main(
                _argv(
                    bundle_path,
                    root,
                    allowlist_dir,
                    extra=("--yes", "--rotation-log", str(rotation_log)),
                )
            )
            == 130
        )
    transaction = _recovery_path(capsys.readouterr().err)
    staged = transaction / "rotation-staged.log"
    text = staged.read_text(encoding="utf-8")
    staged.write_text(
        text.replace('"kind":', '"kind": "tier_model_rotation", "kind":', 1),
        encoding="utf-8",
    )

    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log), "--resume", str(transaction)),
        )
    )

    assert rc == 2
    assert "staged rotation audit" in capsys.readouterr().err


def test_sign_bundle_final_reconciliation_rejects_extra_rotation_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    stale_key = _stale_rotation_key(finding)
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=stale_key)
    before = _tree_bytes(allowlist_dir)
    rotation_log = tmp_path / "rotations.log"
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(
            root,
            allowlist_dir,
            (BundleAction(lane="resign", kind="rotation", key=stale_key, source_file="gadget.yaml"),),
        ),
    )
    real_verify = sign_bundle_transaction._verify_completed_actions
    calls = 0

    def _verify_then_inject(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        real_verify(*args, **kwargs)
        calls += 1
        if calls == 2:
            staged = kwargs["tx_path"] / "rotation-staged.log"
            record = staged.read_text(encoding="utf-8").splitlines()[-1]
            staged.write_text(staged.read_text(encoding="utf-8") + record + "\n", encoding="utf-8")

    monkeypatch.setattr(sign_bundle_transaction, "_verify_completed_actions", _verify_then_inject)
    rc = main(
        _argv(
            bundle_path,
            root,
            allowlist_dir,
            extra=("--yes", "--rotation-log", str(rotation_log)),
        )
    )

    assert rc == 2
    assert _tree_bytes(allowlist_dir) == before
    assert "exactly match completed rotations" in capsys.readouterr().err


def test_sign_bundle_resume_tolerates_checkpoint_created_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    real_save = sign_bundle_transaction.save_manifest
    saves = 0

    def _interrupt_second_save(*args: Any, **kwargs: Any) -> None:
        nonlocal saves
        saves += 1
        if saves == 2:
            raise KeyboardInterrupt
        real_save(*args, **kwargs)

    monkeypatch.setattr(sign_bundle_transaction, "save_manifest", _interrupt_second_save)
    assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    assert (transaction / "checkpoint").is_dir()

    monkeypatch.setattr(sign_bundle_transaction, "save_manifest", real_save)
    with _patch_judge(_accept_all) as calls:
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 0
    assert calls == ["plugins/gadget.py"]


def test_sign_bundle_resume_tolerates_checkpoint_retired_after_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core import sign_bundle_transaction

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/gadget.py", "gadget")
    finding = _live_finding(root, "plugins/gadget.py")
    bundle_path = _write_bundle_file(
        tmp_path,
        _bundle(root, allowlist_dir, (_new_judgment_action(finding, "plugins/gadget.py"),)),
    )
    real_clear = sign_bundle_transaction.clear_action_checkpoint

    def _interrupt_before_delete(_tx_path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sign_bundle_transaction, "clear_action_checkpoint", _interrupt_before_delete)
    with _patch_judge(_accept_all):
        assert main(_argv(bundle_path, root, allowlist_dir, extra=("--yes",))) == 130
    transaction = _recovery_path(capsys.readouterr().err)
    manifest = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
    assert manifest["running_action"] is None
    assert manifest["completed_actions"] == [0]
    assert manifest["checkpoint_snapshot"] is None
    assert (transaction / "checkpoint").is_dir()

    monkeypatch.setattr(sign_bundle_transaction, "clear_action_checkpoint", real_clear)
    with _patch_judge(lambda _file_path: (_ for _ in ()).throw(AssertionError("journaled accepted action must not be re-judged"))):
        rc = main(_argv(bundle_path, root, allowlist_dir, extra=("--yes", "--resume", str(transaction))))

    assert rc == 0
    assert _canonical_key(finding) in (allowlist_dir / "plugins.yaml").read_text(encoding="utf-8")


def test_reaudit_sidecar_writer_blocks_publish_and_is_not_stranded(
    tmp_path: Path,
) -> None:
    from elspeth_lints.core import sign_bundle_transaction
    from elspeth_lints.core.reaudit_sidecar import SidecarHeader, SidecarWriter

    active = _build_allowlist_dir(tmp_path)
    tx_path = tmp_path / "tx"
    candidate = tx_path / "candidate" / active.name
    candidate.parent.mkdir(parents=True)
    shutil.copytree(active, candidate)
    (candidate / "_defaults.yaml").write_text(
        (candidate / "_defaults.yaml").read_text(encoding="utf-8") + "# candidate\n",
        encoding="utf-8",
    )
    manifest = {
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "base_snapshot": sign_bundle_transaction.tree_snapshot(active),
        "candidate_snapshot": sign_bundle_transaction.tree_snapshot(candidate),
    }
    run_id = "a" * 32
    sidecar = active / ".reaudit-state" / f"{run_id}.jsonl"
    header = SidecarHeader(
        run_id=run_id,
        started_at=datetime.now(UTC),
        total_entries=0,
        allowlist_path=str(active),
        allowlist_hash="0" * 64,
        judge_transport="openrouter",
        rule_filter="trust_tier.tier_model",
        since_iso=None,
        limit=None,
        include_pre_judge=False,
    )
    publish_error: list[BaseException] = []
    publish_done = threading.Event()

    def _publish() -> None:
        try:
            sign_bundle_transaction.publish_candidate(tx_path, manifest)
        except BaseException as exc:
            publish_error.append(exc)
        finally:
            publish_done.set()

    with SidecarWriter(sidecar, header):
        publisher = threading.Thread(target=_publish, daemon=True)
        publisher.start()
        assert not publish_done.wait(timeout=0.1)

    publisher.join(timeout=5)
    assert len(publish_error) == 1
    assert "publish precondition failed" in str(publish_error[0])
    assert sidecar.is_file()
    assert not (candidate / ".reaudit-state").exists()
