"""``elspeth-judge`` MCP server -- fail-closed, shape-only, tool-surface tests.

The server is the **key-free** agent surface ([O1] linchpin): every tool fails
closed when ``ELSPETH_JUDGE_METADATA_HMAC_KEY`` is present in its environment, and
no tool ever mints a signature. ``verify_signatures`` is structurally shape-only;
the authoritative recompute path lives on the CLI/library ``diagnose`` surface,
not here.

Fixtures (source tree + signed/pre-judge allowlist YAML) are replicated locally
-- there is no ``tests/unit/elspeth_lints`` package and no precedent for
cross-test imports.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from elspeth_lints.core.allowlist import (
    _JUDGE_METADATA_SIGNATURE_ENV_VAR,
    JudgeVerdict,
    compute_judge_metadata_signature,
)
from elspeth_lints.core.judge import JUDGE_POLICY_HASH, TRANSPORT_CODEX_CLI, JudgeConfigurationError
from elspeth_lints.core.review_bundle import BundleAction, ReviewBundle, read_bundle, write_bundle
from elspeth_lints.mcp import server as judge_server
from elspeth_lints.rules.trust_tier.tier_model.rotate import identity_prefix

_RECORDED_AT = "2024-01-01T00:00:00+00:00"
_MODEL = "claude-opus-4-7"
_RATIONALE = "original judge said the boundary was genuine"


@pytest.fixture(autouse=True)
def _keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    # The MCP surface is structurally key-free; default every test to no key.
    monkeypatch.delenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, raising=False)


# --------------------------------------------------------------------------- #
# Fixture helpers (mirrored from test_bundle_verify.py)
# --------------------------------------------------------------------------- #


def _src(doc: str, *, active: bool = True, fallback: str = "anonymous") -> str:
    body = f'        return payload.get("name", "{fallback}")' if active else '        return "anonymous"'
    return f'"""{doc}"""\n\n\nclass Widget:\n    def lookup(self, payload: dict) -> str:\n{body}\n'


def _build_root(tmp_path: Path) -> Path:
    root = tmp_path / "src_root"
    (root / "plugins").mkdir(parents=True)
    return root


def _write_source(
    root: Path,
    rel: str,
    doc: str,
    *,
    active: bool = True,
    prefix: str = "",
    fallback: str = "anonymous",
) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prefix + _src(doc, active=active, fallback=fallback), encoding="utf-8")
    return target


def _build_allowlist_dir(tmp_path: Path) -> Path:
    allowlist_dir = tmp_path / "allowlist"
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


def _write_signed_v2_entry(
    allowlist_dir: Path,
    yaml_name: str,
    *,
    finding: Any,
    scope_fingerprint: str | None = None,
) -> str:
    key = _canonical_key(finding)
    stored_scope = finding.scope_fingerprint if scope_fingerprint is None else scope_fingerprint
    signature = compute_judge_metadata_signature(
        key=key,
        ast_path=finding.ast_path,
        judge_verdict=JudgeVerdict.ACCEPTED,
        judge_recorded_at=datetime.fromisoformat(_RECORDED_AT),
        judge_model=_MODEL,
        judge_rationale=_RATIONALE,
        judge_policy_hash=JUDGE_POLICY_HASH,
        signature_version=2,
        scope_fingerprint=stored_scope,
        judge_transport="openrouter",
        hmac_key=("x" * 32).encode("utf-8"),
    )
    lines = [
        "allow_hits:",
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
        f"  scope_fingerprint: '{stored_scope}'",
        "  judge_transport: openrouter",
        f"  ast_path: '{finding.ast_path}'",
        f"  judge_metadata_signature: '{signature}'",
    ]
    (allowlist_dir / yaml_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


def _write_pre_judge_entry(allowlist_dir: Path, yaml_name: str, *, key: str) -> str:
    lines = [
        "allow_hits:",
        f"- key: {key}",
        "  owner: test-owner",
        "  reason: |-",
        "    payload is Tier-3 external data from upstream tool-call",
        "  safety: |-",
        "    suppression",
        "  expires: '2030-01-01'",
    ]
    (allowlist_dir / yaml_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


def _context(root: Path, allowlist_dir: Path, staged_dir: Path) -> Any:
    return judge_server._ServerContext(root=root, allowlist_dir=allowlist_dir, staged_dir=staged_dir)


# --------------------------------------------------------------------------- #
# Task 3.1 -- shared fail-closed guard
# --------------------------------------------------------------------------- #


def test_assert_no_hmac_key_in_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    with pytest.raises(judge_server.HmacKeyPresentError):
        judge_server._assert_no_hmac_key_in_env()


def test_assert_no_hmac_key_in_env_returns_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, raising=False)
    assert judge_server._assert_no_hmac_key_in_env() is None


def test_assert_no_hmac_key_message_names_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    with pytest.raises(judge_server.HmacKeyPresentError) as excinfo:
        judge_server._assert_no_hmac_key_in_env()
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Task 3.2 -- verify_signatures + stage_status + structural fail-closed
# --------------------------------------------------------------------------- #


def _status_bundle(root: Path, allowlist_dir: Path, actions: tuple[BundleAction, ...]) -> ReviewBundle:
    return ReviewBundle(
        bundle_id="status-bundle",
        schema_version=1,
        created_at="2026-06-28T00:00:00+00:00",
        staged_by="agent-x",
        root=str(root),
        allowlist_dir=str(allowlist_dir),
        source_rev=None,
        source_dirty=False,
        actions=actions,
    )


def test_verify_signatures_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    ctx = _context(root, allowlist_dir, tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "verify_signatures", {})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text


def test_verify_signatures_shape_only_without_key(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)

    ctx = _context(root, allowlist_dir, tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "verify_signatures", {})
    assert outcome.is_error is False
    payload = json.loads(outcome.text)
    assert payload["verification_mode"] == "shape-only"


def test_stage_status_reads_bundle(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"
    bundle = _status_bundle(
        root,
        allowlist_dir,
        (
            BundleAction(
                lane="new_judgment",
                kind="justify",
                key="plugins/a.py:R1:A:m:fp=aaaa",
                file_path="plugins/a.py",
                symbol="A.m",
                rule="R1",
                fingerprint="aaaa",
            ),
            BundleAction(lane="resign", kind="drift_repair", key="k1:fp=bbbb", diagnosis_status="AST_PATH_BINDING_DRIFT"),
            BundleAction(lane="resign", kind="rotation", key="k2:fp=cccc", source_file="plugins.yaml"),
            BundleAction(lane="resign", kind="stale_delete", key="k3:fp=dddd", source_file="plugins.yaml"),
        ),
    )
    written = write_bundle(bundle, staged_dir=staged_dir)

    ctx = _context(root, allowlist_dir, staged_dir)
    outcome = judge_server._run_tool(ctx, "stage_status", {"bundle_id": "status-bundle"})
    assert outcome.is_error is False
    payload = json.loads(outcome.text)
    assert payload["actions_total"] == 4
    assert payload["kind_counts"]["justify"] == 1
    assert payload["kind_counts"]["drift_repair"] == 1
    assert payload["kind_counts"]["rotation"] == 1
    assert payload["kind_counts"]["stale_delete"] == 1
    assert payload["lane_counts"]["new_judgment"] == 1
    assert payload["lane_counts"]["resign"] == 3
    # Paste-ready operator command names the sign-bundle subcommand + the bundle file.
    assert "sign-bundle" in payload["sign_bundle_command"]
    assert str(written) in payload["sign_bundle_command"]
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in payload["sign_bundle_command"]
    assert "--judge-transport codex-cli" in payload["sign_bundle_command"]
    assert "--judge-tools readonly" in payload["sign_bundle_command"]
    assert "--dry-run" in payload["sign_bundle_command"]


def test_stage_status_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "stage_status", {"bundle_id": "anything"})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text


def test_every_registered_tool_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural invariant #2 ([O1]): with the key present, EVERY registered
    tool fails closed -- a future tool added without routing through
    ``_assert_no_hmac_key_in_env()`` is caught here even if its hand-written
    per-tool test stays green.

    Both assertions are load-bearing: ``is_error`` alone is vacuous for the
    arg-requiring tools (a removed guard would ``KeyError`` on the missing arg
    and still set ``is_error``). Requiring the env-var name in the text means a
    guard-removed handler -- which would instead emit an arg error, a success
    JSON, or an SDK install hint, none of which name the key -- fails the test.
    """
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    assert judge_server._TOOLS, "no tools registered"
    for name in judge_server._TOOLS:
        outcome = judge_server._run_tool(ctx, name, {})
        assert outcome.is_error is True, f"{name} did not fail closed with the key present"
        assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text, f"{name} fail-closed text must name the key env var"


# --------------------------------------------------------------------------- #
# Task 3.3 -- stage_scan
# --------------------------------------------------------------------------- #

_SHIFT = "_SHIM = 1\n\n\n"  # AST-position cascade: prepended module-body statement.


def _scan_and_read(ctx: Any, bundle_id: str) -> ReviewBundle:
    outcome = judge_server._run_tool(ctx, "stage_scan", {"bundle_id": bundle_id})
    assert outcome.is_error is False, outcome.text
    payload = json.loads(outcome.text)
    bundle = read_bundle(Path(payload["written_path"]))
    return bundle


def test_stage_scan_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "stage_scan", {"bundle_id": "scan"})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text


def test_stage_scan_builds_bundle(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    # drift_repair: a signed entry whose stored scope no longer matches the tree.
    _write_source(root, "plugins/widget.py", "widget")
    drift_finding = _live_finding(root, "plugins/widget.py")
    drift_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=drift_finding, scope_fingerprint="b" * 64)
    # new_judgment: a live, uncovered finding (no allowlist entry shares its prefix).
    _write_source(root, "plugins/gadget.py", "gadget")
    gadget_finding = _live_finding(root, "plugins/gadget.py")
    gadget_key = _canonical_key(gadget_finding)

    ctx = _context(root, allowlist_dir, staged_dir)
    outcome = judge_server._run_tool(ctx, "stage_scan", {"bundle_id": "scan-1"})
    assert outcome.is_error is False
    payload = json.loads(outcome.text)
    assert payload["kind_counts"].get("drift_repair") == 1
    assert payload["kind_counts"].get("justify") == 1
    assert payload["target_census"] == {
        "raw_target_count": 2,
        "exact_covered_count": 1,
        "per_file_covered_count": 0,
        "uncovered_count": 1,
    }
    assert "sign-bundle" in payload["sign_bundle_command"]

    bundle = read_bundle(Path(payload["written_path"]))
    drift = [a for a in bundle.actions if a.kind == "drift_repair"]
    new = [a for a in bundle.actions if a.kind == "justify"]
    assert len(drift) == 1 and len(new) == 1
    assert drift[0].key == drift_key
    assert drift[0].diagnosis_status == "SCOPE_BINDING_DRIFT"
    assert new[0].key == gadget_key
    assert new[0].lane == "new_judgment"
    assert new[0].file_path == "plugins/gadget.py"
    assert new[0].symbol == "Widget.lookup"
    # No rotation action -- the only judge-gated entry is filtered out of the scan.
    assert all(a.kind != "rotation" for a in bundle.actions)


def test_stage_scan_records_absolute_scope_from_relative_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = Path("src_root")
    root.mkdir()
    allowlist_dir = Path("allowlist")
    allowlist_dir.mkdir()
    (allowlist_dir / "_defaults.yaml").write_text(
        "version: 1\ndefaults:\n  fail_on_stale: false\n  fail_on_expired: false\n",
        encoding="utf-8",
    )
    ctx = _context(root, allowlist_dir, Path("staged"))

    bundle = _scan_and_read(ctx, "absolute-scope")

    assert Path(bundle.root) == root.resolve()
    assert Path(bundle.allowlist_dir) == allowlist_dir.resolve()
    assert Path(bundle.root).is_absolute()
    assert Path(bundle.allowlist_dir).is_absolute()


def test_stage_scan_rejects_ambiguous_non_judge_target_group(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"
    target = root / "plugins" / "gadget.py"
    target.write_text(
        "class Widget:\n"
        "    def lookup(self, payload: dict) -> str:\n"
        "        first = payload.get('first', 'anonymous')\n"
        "        return payload.get('second', first)\n",
        encoding="utf-8",
    )
    from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file

    findings = [finding for finding in scan_file(target.resolve(), root) if finding.rule_id == "R1"]
    assert len(findings) == 2
    _write_pre_judge_entry(allowlist_dir, "gadget.yaml", key=_canonical_key(findings[0]))
    ctx = _context(root, allowlist_dir, staged_dir)

    outcome = judge_server._run_tool(ctx, "stage_scan", {"bundle_id": "ambiguous"})

    assert outcome.is_error is True
    assert "ambiguous non-judge target group" in outcome.text
    assert not staged_dir.exists()


def test_stage_scan_accepts_mixed_signed_and_prejudge_exact_same_prefix_group(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"
    target = root / "plugins" / "gadget.py"
    target.write_text(
        "class Widget:\n"
        "    def lookup(self, payload: dict) -> str:\n"
        "        first = payload.get('first', 'anonymous')\n"
        "        return payload.get('second', first)\n",
        encoding="utf-8",
    )
    from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file

    findings = [finding for finding in scan_file(target.resolve(), root) if finding.rule_id == "R1"]
    assert len(findings) == 2
    _write_signed_v2_entry(allowlist_dir, "signed.yaml", finding=findings[0])
    _write_pre_judge_entry(allowlist_dir, "prejudge.yaml", key=_canonical_key(findings[1]))
    ctx = _context(root, allowlist_dir, staged_dir)

    bundle = _scan_and_read(ctx, "mixed-exact")

    assert bundle.actions == ()


def test_stage_scan_does_not_stage_per_file_rule_covered_finding(tmp_path: Path) -> None:
    """Production-covered findings must not become redundant judgments."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    _write_source(root, "plugins/covered.py", "covered")
    _write_source(root, "plugins/uncovered.py", "uncovered")
    uncovered_key = _canonical_key(_live_finding(root, "plugins/uncovered.py"))
    (allowlist_dir / "plugins.yaml").write_text(
        "\n".join(
            [
                "per_file_rules:",
                "- pattern: plugins/covered.py",
                "  rules: [R1]",
                "  reason: existing production suppression",
                "  expires: '2030-01-01'",
                "  max_hits: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-per-file-covered")

    new_keys = [action.key for action in bundle.actions if action.kind == "justify"]
    assert new_keys == [uncovered_key]


def test_stage_scan_fp_shifted_judge_gated_drift_does_not_raise_and_routes_to_drift_repair_only(
    tmp_path: Path,
) -> None:
    """BLOCKING regression pin (AST-position cascade primary scenario).

    A judge-gated v2 entry whose module body is shifted (a prepended statement)
    so the leading ``ast_path`` index and the finding's ``:fp=`` suffix shift,
    while the enclosing scope content stays byte-identical. Against the pre-fix
    UNFILTERED whole-dir ``scan_for_rotations`` this would ``RuntimeError`` at
    ``plan_rotations`` (1 finding + 1 judge-gated entry with ``new_key !=
    old_key`` -> ``_refuse_rotation_of_judge_gated_entry``). The fixed
    ``exclude_judge_gated=True`` scan filters it out, so stage_scan does NOT
    raise; ``diagnose`` rescues the entry via the v2 scope fallback and reports
    ``AST_PATH_BINDING_DRIFT`` -> ``drift_repair`` ONLY (no rotation, no
    new_judgment for the shifted live finding).
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    _write_source(root, "plugins/widget.py", "widget")
    finding = _live_finding(root, "plugins/widget.py")
    drift_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=finding)
    # Shift the module body (leading ast_path index + :fp= shift; scope identical).
    _write_source(root, "plugins/widget.py", "widget", prefix=_SHIFT)

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-shift")  # (a) does NOT raise

    drift = [a for a in bundle.actions if a.kind == "drift_repair"]
    # (b) drift_repair for the OLD key, status AST_PATH_BINDING_DRIFT (signable)
    assert len(drift) == 1
    assert drift[0].key == drift_key
    assert drift[0].diagnosis_status == "AST_PATH_BINDING_DRIFT"
    from elspeth_lints.core.judge_signature_diagnosis import _SIGNABLE_DIAGNOSIS_STATUSES

    assert "AST_PATH_BINDING_DRIFT" in _SIGNABLE_DIAGNOSIS_STATUSES
    # (c) no rotation for that key
    assert all(a.kind != "rotation" for a in bundle.actions)
    # (d) no new_judgment for the shifted live finding (double-route guard)
    new_prefixes = {identity_prefix(a.key) for a in bundle.actions if a.kind == "justify"}
    assert identity_prefix(drift_key) not in new_prefixes
    assert all(a.kind != "justify" for a in bundle.actions)


def test_stage_scan_unique_same_prefix_semantic_replacement_routes_to_authoritative_rejudgment(
    tmp_path: Path,
) -> None:
    """A unique semantic replacement must not be reduced to stale deletion.

    Changing the call's default changes both the finding fingerprint and its
    enclosing scope fingerprint while preserving file/rule/symbol identity.
    The old signed row therefore has no exact or scope-fallback match, but the
    sole live same-prefix finding is a deterministic replacement candidate.
    It must travel through the re-judge lane; no signed authority is carried
    mechanically to the replacement key.
    """
    from elspeth_lints.core.bundle_verify import verify_bundle_against_tree
    from elspeth_lints.core.judge_signature_diagnosis import diagnose_judge_signatures

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    _write_source(root, "plugins/widget.py", "widget")
    old_finding = _live_finding(root, "plugins/widget.py")
    old_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=old_finding)

    _write_source(root, "plugins/widget.py", "widget", fallback="guest")
    replacement = _live_finding(root, "plugins/widget.py")
    replacement_key = _canonical_key(replacement)
    assert replacement_key != old_key
    assert identity_prefix(replacement_key) == identity_prefix(old_key)
    assert replacement.scope_fingerprint != old_finding.scope_fingerprint

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-semantic-replacement")

    drift = [action for action in bundle.actions if action.kind == "drift_repair"]
    assert len(drift) == 1
    assert drift[0].key == old_key
    assert drift[0].diagnosis_status == "IDENTITY_PREFIX_REPLACEMENT"
    assert drift[0].draft_rationale is None
    assert drift[0].preview is None
    assert all(action.kind != "stale_delete" for action in bundle.actions)
    assert all(action.kind != "justify" for action in bundle.actions)

    verification = verify_bundle_against_tree(bundle, root=root, allowlist_dir=allowlist_dir)
    assert verification.ok, verification.mismatches

    diagnosis = diagnose_judge_signatures(root=root, allowlist_dir=allowlist_dir)
    item = next(item for item in diagnosis.items if item.key == old_key)
    assert item.status == "IDENTITY_PREFIX_REPLACEMENT"
    assert item.repair_key == replacement_key


def test_stage_scan_ambiguous_same_prefix_semantic_replacement_uses_two_cycle_path(tmp_path: Path) -> None:
    """Multiple live candidates require deletion before fresh judgments surface."""
    from elspeth_lints.core.judge_signature_diagnosis import diagnose_judge_signatures
    from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file

    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    target = _write_source(root, "plugins/widget.py", "widget")
    old_finding = _live_finding(root, "plugins/widget.py")
    old_key = _write_signed_v2_entry(allowlist_dir, "widget.yaml", finding=old_finding)

    target.write_text(
        '"""widget"""\n\n\nclass Widget:\n'
        "    def lookup(self, payload: dict) -> str:\n"
        '        return payload.get("name", "guest") + payload.get("alias", "guest")\n',
        encoding="utf-8",
    )
    replacements = [finding for finding in scan_file(target.resolve(), root) if finding.rule_id == "R1"]
    assert len(replacements) == 2
    assert {identity_prefix(_canonical_key(finding)) for finding in replacements} == {identity_prefix(old_key)}

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-ambiguous-replacement-cycle-1")
    stale = [action for action in bundle.actions if action.kind == "stale_delete"]
    assert [action.key for action in stale] == [old_key]
    assert all(action.kind != "drift_repair" for action in bundle.actions)
    assert all(action.kind != "justify" for action in bundle.actions)

    diagnosis = diagnose_judge_signatures(root=root, allowlist_dir=allowlist_dir)
    item = next(item for item in diagnosis.items if item.key == old_key)
    assert item.status == "NO_MATCHING_FINDING"
    assert item.repair_key is None

    # Cycle two: only after the stale authority is removed do both uncovered
    # live findings enter the fresh-judgment lane.
    (allowlist_dir / "widget.yaml").unlink()
    cycle_two = _scan_and_read(ctx, "scan-ambiguous-replacement-cycle-2")
    new_keys = {action.key for action in cycle_two.actions if action.kind == "justify"}
    assert new_keys == {_canonical_key(finding) for finding in replacements}


def test_stage_scan_multiple_stale_rows_for_one_semantic_replacement_uses_two_cycle_path(tmp_path: Path) -> None:
    """A live replacement cannot pair to more than one signed old row."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    _write_source(root, "plugins/widget.py", "widget")
    first_finding = _live_finding(root, "plugins/widget.py")
    first_key = _write_signed_v2_entry(allowlist_dir, "first.yaml", finding=first_finding)

    _write_source(root, "plugins/widget.py", "widget", fallback="legacy")
    second_finding = _live_finding(root, "plugins/widget.py")
    second_key = _write_signed_v2_entry(allowlist_dir, "second.yaml", finding=second_finding)

    _write_source(root, "plugins/widget.py", "widget", fallback="guest")
    replacement_key = _canonical_key(_live_finding(root, "plugins/widget.py"))
    assert len({first_key, second_key, replacement_key}) == 3
    assert len({identity_prefix(first_key), identity_prefix(second_key), identity_prefix(replacement_key)}) == 1

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-ambiguous-stale-rows")

    stale_keys = {action.key for action in bundle.actions if action.kind == "stale_delete"}
    assert stale_keys == {first_key, second_key}
    assert all(action.kind != "drift_repair" for action in bundle.actions)
    assert all(action.kind != "justify" for action in bundle.actions)


def test_stage_scan_new_judgment_and_drift_repair_prefixes_are_disjoint(tmp_path: Path) -> None:
    """General disjointness invariant across all three entry populations.

    Fails if the new_judgment coverage check is run against the
    ``exclude_judge_gated``-filtered set: the drifted judge-gated site would then
    look 'uncovered' and double-route (a ``new_judgment`` at its new key AND a
    ``drift_repair`` at its old key, sharing one identity-prefix).
    """
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    # (1) judge-gated OK (signed, no drift) -> no action.
    _write_source(root, "plugins/ok.py", "ok")
    ok_finding = _live_finding(root, "plugins/ok.py")
    _write_signed_v2_entry(allowlist_dir, "ok.yaml", finding=ok_finding)
    # (2) judge-gated drifted (fp-shifted) -> drift_repair.
    _write_source(root, "plugins/drift.py", "drift")
    drift_finding = _live_finding(root, "plugins/drift.py")
    _write_signed_v2_entry(allowlist_dir, "drift.yaml", finding=drift_finding)
    _write_source(root, "plugins/drift.py", "drift", prefix=_SHIFT)
    # (3) non-judge-gated (pre-judge) entry matching its live finding -> no action.
    _write_source(root, "plugins/prejudge.py", "prejudge")
    prejudge_finding = _live_finding(root, "plugins/prejudge.py")
    _write_pre_judge_entry(allowlist_dir, "prejudge.yaml", key=_canonical_key(prejudge_finding))
    # (4) genuinely new uncovered finding -> new_judgment.
    _write_source(root, "plugins/fresh.py", "fresh")

    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "scan-disjoint")

    new_prefixes = {identity_prefix(a.key) for a in bundle.actions if a.kind == "justify"}
    drift_prefixes = {identity_prefix(a.key) for a in bundle.actions if a.kind == "drift_repair"}
    assert new_prefixes, "expected at least one new_judgment action"
    assert drift_prefixes, "expected at least one drift_repair action"
    assert new_prefixes.isdisjoint(drift_prefixes)
    # The fresh finding routes to new_judgment; the drifted judge-gated site does not.
    assert any("plugins/fresh.py" in p for p in new_prefixes)
    assert any("plugins/drift.py" in p for p in drift_prefixes)
    assert not any("plugins/drift.py" in p for p in new_prefixes)


# --------------------------------------------------------------------------- #
# Task 3.4 -- stage_preview (non-authoritative agent judge)
# --------------------------------------------------------------------------- #


def _fake_response(verdict: JudgeVerdict, rationale: str) -> SimpleNamespace:
    return SimpleNamespace(
        verdict=verdict,
        judge_rationale=rationale,
        model_id="codex-preview-model",
        judge_transport=TRANSPORT_CODEX_CLI,
    )


def _staged_new_judgment_bundle(tmp_path: Path) -> tuple[Any, Path, str]:
    """Build a fixture whose stage_scan yields exactly one ``justify`` action."""
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"
    _write_source(root, "plugins/gadget.py", "gadget")
    ctx = _context(root, allowlist_dir, staged_dir)
    bundle = _scan_and_read(ctx, "preview-base")
    assert any(a.kind == "justify" for a in bundle.actions)
    return ctx, staged_dir, "preview-base"


def test_stage_preview_fills_non_authoritative_verdicts(tmp_path: Path) -> None:
    ctx, staged_dir, bundle_id = _staged_new_judgment_bundle(tmp_path)
    sentinel_scope = object()

    with (
        patch("elspeth_lints.core.judge.build_readonly_tool_scope", return_value=sentinel_scope) as mock_scope,
        patch(
            "elspeth_lints.core.judge.call_judge",
            return_value=_fake_response(JudgeVerdict.ACCEPTED, "boundary is genuine"),
        ) as mock_call,
    ):
        outcome = judge_server._run_tool(ctx, "stage_preview", {"bundle_id": bundle_id})

    assert outcome.is_error is False, outcome.text
    # Read-only agent posture (defense-in-depth): the patched judge was reached
    # via the agent transport with exactly the read-only tool scope.
    assert mock_scope.called
    assert mock_call.call_args.kwargs["transport"] == TRANSPORT_CODEX_CLI
    assert mock_call.call_args.kwargs["tool_scope"] is sentinel_scope

    bundle = read_bundle(staged_dir / f"{bundle_id}.json")
    previews = [a.preview for a in bundle.actions if a.kind == "justify"]
    assert previews and all(p is not None for p in previews)
    assert all(p.authoritative is False for p in previews)
    assert all(p.verdict == "ACCEPTED" for p in previews)
    assert all(p.transport == TRANSPORT_CODEX_CLI for p in previews)
    # Bundle still serializes no signature.
    text = (staged_dir / f"{bundle_id}.json").read_text(encoding="utf-8")
    assert "judge_metadata_signature" not in text
    assert "hmac-sha256:" not in text


def test_stage_preview_surfaces_blocked_reason(tmp_path: Path) -> None:
    ctx, staged_dir, bundle_id = _staged_new_judgment_bundle(tmp_path)

    with (
        patch("elspeth_lints.core.judge.build_readonly_tool_scope", return_value=object()),
        patch(
            "elspeth_lints.core.judge.call_judge",
            return_value=_fake_response(JudgeVerdict.BLOCKED, "rationale does not address the rule"),
        ),
    ):
        outcome = judge_server._run_tool(ctx, "stage_preview", {"bundle_id": bundle_id})

    assert outcome.is_error is False, outcome.text
    payload = json.loads(outcome.text)
    blocked_keys = {entry["key"] for entry in payload["blocked"]}
    assert blocked_keys, "stage_preview must surface the BLOCKED action(s) to the agent"

    bundle = read_bundle(staged_dir / f"{bundle_id}.json")
    blocked_previews = [a.preview for a in bundle.actions if a.kind == "justify"]
    assert blocked_previews and all(p is not None for p in blocked_previews)
    assert all(p.verdict == "BLOCKED" for p in blocked_previews)
    assert all(p.rationale == "rationale does not address the rule" for p in blocked_previews)
    assert all(p.authoritative is False for p in blocked_previews)
    text = (staged_dir / f"{bundle_id}.json").read_text(encoding="utf-8")
    assert "judge_metadata_signature" not in text
    assert "hmac-sha256:" not in text


def test_stage_preview_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "stage_preview", {"bundle_id": "anything"})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text


def test_stage_preview_missing_codex_cli_returns_actionable_hint(tmp_path: Path) -> None:
    """Key absent + Codex CLI absent yields an actionable configuration error."""
    ctx, _staged_dir, bundle_id = _staged_new_judgment_bundle(tmp_path)
    codex_absent = JudgeConfigurationError("The Codex CLI is required for --judge-transport codex-cli.")
    with (
        patch("elspeth_lints.core.judge.build_readonly_tool_scope", return_value=object()),
        patch("elspeth_lints.core.judge.call_judge", side_effect=codex_absent),
    ):
        outcome = judge_server._run_tool(ctx, "stage_preview", {"bundle_id": bundle_id})
    assert outcome.is_error is True
    assert "Codex CLI" in outcome.text


def test_stage_preview_still_fails_closed_with_judge_agent_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[O1] ordering: the key-check fires BEFORE the lazy claude-agent-sdk import.

    Simulate the ``[judge-agent]`` extra being absent (``call_judge`` raises
    ModuleNotFoundError) AND the key present: the result must be the fail-closed
    key error, NOT the install hint -- proving the structural guarantee does not
    silently depend on the optional extra being installed.
    """
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    with (
        patch("elspeth_lints.core.judge.build_readonly_tool_scope", side_effect=ModuleNotFoundError("No module named 'claude_agent_sdk'")),
        patch("elspeth_lints.core.judge.call_judge", side_effect=ModuleNotFoundError("No module named 'claude_agent_sdk'")),
    ):
        outcome = judge_server._run_tool(ctx, "stage_preview", {"bundle_id": "anything"})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text
    assert "claude-agent-sdk" not in outcome.text
    assert "judge-agent" not in outcome.text


# --------------------------------------------------------------------------- #
# stage_rekey (Task 4.2 -- the MCP half: enumerate valid, flag broken)
# --------------------------------------------------------------------------- #


def test_stage_rekey_lists_valid_and_flags_broken(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    allowlist_dir = _build_allowlist_dir(tmp_path)
    staged_dir = tmp_path / "staged"

    # valid: a signed v2 entry whose binding matches the tree.
    _write_source(root, "plugins/ok.py", "ok")
    ok_finding = _live_finding(root, "plugins/ok.py")
    ok_key = _write_signed_v2_entry(allowlist_dir, "ok.yaml", finding=ok_finding)
    # broken: a signed entry whose stored scope no longer matches the tree.
    _write_source(root, "plugins/drift.py", "drift")
    drift_finding = _live_finding(root, "plugins/drift.py")
    drift_key = _write_signed_v2_entry(allowlist_dir, "drift.yaml", finding=drift_finding, scope_fingerprint="b" * 64)

    ctx = _context(root, allowlist_dir, staged_dir)
    outcome = judge_server._run_tool(
        ctx,
        "stage_rekey",
        {"old_key_env": "OLD_JUDGE_KEY", "new_key_env": "NEW_JUDGE_KEY", "bundle_id": "rekey-1"},
    )
    assert outcome.is_error is False, outcome.text

    bundle = read_bundle(staged_dir / "rekey-1.json")
    assert bundle.actions == ()
    assert bundle.rekey is not None
    assert bundle.rekey.old_key_env == "OLD_JUDGE_KEY"
    assert bundle.rekey.new_key_env == "NEW_JUDGE_KEY"
    assert ok_key in bundle.rekey.keys
    assert drift_key not in bundle.rekey.keys
    assert drift_key in bundle.rekey.broken_keys
    assert ok_key not in bundle.rekey.broken_keys
    # Only env-var NAMES are recorded -- never key bytes.
    text = (staged_dir / "rekey-1.json").read_text(encoding="utf-8")
    assert "judge_metadata_signature" not in text
    assert "hmac-sha256:" not in text


def test_stage_rekey_fails_closed_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_JUDGE_METADATA_SIGNATURE_ENV_VAR, "x" * 32)
    ctx = _context(_build_root(tmp_path), _build_allowlist_dir(tmp_path), tmp_path / "staged")
    outcome = judge_server._run_tool(ctx, "stage_rekey", {"old_key_env": "OLD_JUDGE_KEY", "new_key_env": "NEW_JUDGE_KEY"})
    assert outcome.is_error is True
    assert _JUDGE_METADATA_SIGNATURE_ENV_VAR in outcome.text
